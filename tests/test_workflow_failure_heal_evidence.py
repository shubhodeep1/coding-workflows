#!/usr/bin/env python3
"""Tests for scripts/workflow_failure_heal_evidence.py (heal evidence bundle)."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "workflow_failure_heal_evidence.py"

_spec = importlib.util.spec_from_file_location("workflow_failure_heal_evidence", SCRIPT)
assert _spec and _spec.loader
ev = importlib.util.module_from_spec(_spec)
sys.modules["workflow_failure_heal_evidence"] = ev
_spec.loader.exec_module(ev)

REPO = "acme/coding-workflows"
HEAD_SHA = "a" * 40
MERGE_SHA = "b" * 40
CYAN = "\x1b[36;1m"


def _ts(second: int) -> str:
	return f"2026-10-04T01:{second // 60:02d}:{second % 60:02d}.1234567Z"


def _review_log() -> str:
	"""A review job log shaped like run 37166027253 (issue #6055)."""
	lines = [
		f"{_ts(0)} ##[group]Run actions/checkout@v4",
		f"{_ts(1)} ##[endgroup]",
		f"{_ts(10)} ##[group]Run bash scripts/review_apply_fixes.sh",
		f"{_ts(10)} {CYAN}bash scripts/review_apply_fixes.sh  # echoed script line",
		f"{_ts(10)} shell: /usr/bin/bash -e {{0}}",
		f"{_ts(10)} env:",
		f"{_ts(10)}   GIT_DIR: /home/runner/work/cw/cw/.git",
		f"{_ts(10)}   GIT_WORK_TREE: /home/runner/work/_temp/workspaces/6133-1",
		f"{_ts(10)} ##[endgroup]",
		f"{_ts(11)} ##[warning]Editor claimed changes but git shows no substantive diff from HEAD on attempt 1.",
	]
	lines += [f"{_ts(12)} noise line {i}" for i in range(300)]
	lines += [
		f"{_ts(20)} ##[group]Working tree state (checkpoint=commit_step_start)",
		f"{_ts(20)} (clean)",
		f"{_ts(20)} ##[endgroup]",
		f"{_ts(21)} Editor-touched files (0):",
		f"{_ts(22)} ##[group]Run bash scripts/review_autofix_step_editor_uncommitted_changes.sh",
		f"{_ts(22)} {CYAN}echo '::error::this echoed error must not count'",
		f"{_ts(22)} env:",
		f"{_ts(22)}   GIT_WORK_TREE: /home/runner/work/_temp/workspaces/6133-1",
		f"{_ts(22)}   EDITOR_CHANGES_LOST: true",
		f"{_ts(22)} ##[endgroup]",
		f"{_ts(23)} ##[error]Editor claimed changes but no commit was produced.",
	]
	lines += [f"{_ts(24)} trailing line {i}" for i in range(200)]
	lines.append(f"{_ts(25)} REVIEW_AUTOFIX_RUN_SUMMARY_V1 {{\"finalize_reason\":\"editor_changes_lost\"}}")
	return "\n".join(lines) + "\n"


STEPS = [
	{"number": 1, "name": "Checkout", "conclusion": "success", "started_at": "2026-10-04T01:00:00Z", "completed_at": "2026-10-04T01:00:09Z"},
	{"number": 2, "name": "Apply fixes with editor model", "conclusion": "success", "started_at": "2026-10-04T01:00:10Z", "completed_at": "2026-10-04T01:00:21Z"},
	{"number": 3, "name": "Detect editor-claimed-but-uncommitted changes", "conclusion": "success", "started_at": "2026-10-04T01:00:22Z", "completed_at": "2026-10-04T01:00:25Z"},
]


# ---------------------------------------------------------------------------
# slice_job_log
# ---------------------------------------------------------------------------


def test_slice_keeps_the_decisive_lines_filter_log_dropped() -> None:
	sliced = ev.slice_job_log(_review_log(), steps=STEPS)
	# Step table, the first real error with its window, the env of the step
	# that raised it, the checkpoint group and the run summary all survive.
	assert "| 3 | Detect editor-claimed-but-uncommitted changes | success |" in sliced
	assert "##[error]Editor claimed changes but no commit was produced." in sliced
	assert "## Env of the step that raised the first error" in sliced
	assert "GIT_WORK_TREE: /home/runner/work/_temp/workspaces/6133-1" in sliced
	assert "Working tree state (checkpoint=commit_step_start)" in sliced and "(clean)" in sliced
	assert "Editor-touched files (0):" in sliced
	assert "REVIEW_AUTOFIX_RUN_SUMMARY_V1" in sliced
	assert "claimed changes but git shows no substantive diff" in sliced
	# The echoed step script is not output, so its ::error:: text is dropped.
	assert "this echoed error must not count" not in sliced
	assert "\x1b" not in sliced
	# Error windows are labelled with the step that printed them.
	assert 'step 3 "Detect editor-claimed-but-uncommitted changes"' in sliced


def test_repeated_step_env_does_not_crowd_out_the_checkpoints() -> None:
	"""Every step header repeats the job env; 500 steps of AUTOFIX_* env lines
	must not fill the diagnostics quota before the real checkpoint (run
	37166027253 had the checkpoints ~19,000 lines in)."""
	header = []
	for n in range(500):
		header += [
			f"{_ts(1)} ##[group]Run step {n}",
			f"{_ts(1)} env:",
			f"{_ts(1)}   AUTOFIX_FAILURE_HEAD_SHA: {HEAD_SHA}",
			f"{_ts(1)} ##[endgroup]",
			f"{_ts(1)} AUTOFIX_GATE pr=1 head={HEAD_SHA}",
		]
	text = "\n".join(header) + "\n" + _review_log()
	sliced = ev.slice_job_log(text, steps=STEPS)
	assert "Working tree state (checkpoint=commit_step_start)" in sliced
	assert sliced.count("AUTOFIX_GATE pr=1") == 1
	assert "AUTOFIX_FAILURE_HEAD_SHA" not in sliced.split("## Diagnostic groups and lines", 1)[1].split("## Last", 1)[0]


def test_slice_respects_the_byte_budget_and_keeps_errors_first() -> None:
	sliced = ev.slice_job_log(_review_log(), steps=STEPS, max_bytes=4000)
	assert len(sliced.encode("utf-8")) <= 4000
	assert "##[error]Editor claimed changes but no commit was produced." in sliced


def test_tail_clip_keeps_utf8_boundaries_and_tiny_limits() -> None:
	assert ev._clip_bytes("abc", 0, keep="tail") == ""
	assert len(ev._clip_bytes("abc", 1, keep="tail").encode()) == 1
	limit = len("\n[... truncated ...]\n".encode()) + 2
	tail = ev._clip_bytes("a" * limit + "éz", limit, keep="tail")
	assert tail == "\n[... truncated ...]\nz"
	assert len(tail.encode()) <= limit


def test_slice_without_errors_or_steps_still_returns_the_tail() -> None:
	text = "\n".join(f"{_ts(1)} line {i}" for i in range(500))
	sliced = ev.slice_job_log(text)
	assert "line 499" in sliced and "line 10\n" not in sliced
	assert "## Error windows" not in sliced


# ---------------------------------------------------------------------------
# Trust, eligibility, run references
# ---------------------------------------------------------------------------


def _issue(**overrides):
	body = (
		"<!-- workflow-failure-heal:fp=" + "f" * 64 + " -->\n"
		"<!-- workflow-failure-heal:gen=1 -->\n"
		"<!-- workflow-failure-heal:root=" + "f" * 64 + " -->\n"
		f"<!-- workflow-failure-heal:source={REPO}#6133 -->\n"
		f"<!-- workflow-failure-heal:runs={REPO}:111 -->\n"
		"- **Target branch:** `main`\n"
		f"- **Head SHA:** `{HEAD_SHA}`\n"
		"- **Failed on branch:** `ai/issue-5144`\n"
		"- **Failure reason:** `editor_changes_lost`\n"
		f"- **Heal intake run:** https://github.com/{REPO}/actions/runs/999\n"
	)
	issue = {
		"number": 7000,
		"body": body,
		"labels": [{"name": "ai:workflow-heal"}],
		"user": {"login": "healer", "type": "User"},
		"author_association": "OWNER",
	}
	issue.update(overrides)
	return issue


def test_eligibility_requires_label_marker_and_trusted_author() -> None:
	assert ev.eligibility(_issue()) == (True, "eligible")
	assert ev.eligibility(_issue(labels=[]))[1] == "not_heal_issue"
	assert ev.eligibility(_issue(body="no marker"))[1] == "no_heal_marker"
	assert ev.eligibility(_issue(author_association="NONE"))[1] == "untrusted_issue_author"
	bot = _issue(user={"login": "github-actions[bot]", "type": "Bot"}, author_association="NONE")
	assert ev.eligibility(bot)[0] is True
	other_bot = _issue(user={"login": "evil[bot]", "type": "Bot"}, author_association="NONE")
	assert ev.eligibility(other_bot)[1] == "untrusted_issue_author"
	assert ev.eligibility(None)[1] == "issue_unreadable"


def test_run_refs_come_only_from_trusted_sources() -> None:
	occurrence = "<!-- workflow-failure-heal:occurrence -->\n- **Failed run:** https://github.com/{repo}/actions/runs/{run}\n"
	comments = [
		{"user": {"login": "healer"}, "body": occurrence.format(repo=REPO, run=222)},
		# Another author cannot add a run, even with the marker.
		{"user": {"login": "mallory"}, "body": occurrence.format(repo=REPO, run=333)},
		# A run in an unrelated repository is never fetched.
		{"user": {"login": "healer"}, "body": occurrence.format(repo="evil/repo", run=444)},
		# Without the occurrence marker a link is just text.
		{"user": {"login": "healer"}, "body": f"see https://github.com/{REPO}/actions/runs/555"},
		{"user": {"login": "healer"}, "body": occurrence.format(repo=REPO, run=666)},
	]
	refs = ev.trusted_run_refs(_issue(), comments, allowed_repos=[REPO], limit=3)
	assert [ref["run_id"] for ref in refs] == ["111", "222", "666"]
	# The heal intake run linked in the body is not a failing run.
	assert all(ref["run_id"] != "999" for ref in refs)
	assert [ref["run_id"] for ref in ev.trusted_run_refs(_issue(), comments, allowed_repos=[REPO], limit=2)] == ["222", "666"]


def test_occurrence_comment_requires_a_known_issue_author() -> None:
	issue = _issue(user={"type": "User"})
	comment = {
		"body": f"<!-- workflow-failure-heal:occurrence -->\n- **Failed run:** https://github.com/{REPO}/actions/runs/222",
		"user": {},
	}
	assert [ref["run_id"] for ref in ev.trusted_run_refs(issue, [comment], allowed_repos=[REPO], limit=3)] == ["111"]


def test_edited_source_marker_cannot_fetch_unregistered_repository(tmp_path: Path, monkeypatch) -> None:
	registry = tmp_path / "consumers.json"
	registry.write_text(json.dumps(["acme/registered"]))
	monkeypatch.setenv("WORKFLOW_HEAL_CONSUMER_REGISTRY", str(registry))
	issue = _issue(body=_issue()["body"].replace(REPO, "private/other"))
	fake = FakeGh()
	manifest = _collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert {"part": "source_repo", "reason": "not_registered"} in manifest["skipped"]
	assert not any("private/other" in path for path in fake.paths)
	assert "no_trusted_run_links" in {item["reason"] for item in manifest["skipped"]}


def test_registered_consumer_source_remains_readable(tmp_path: Path, monkeypatch) -> None:
	registry = tmp_path / "consumers.json"
	registry.write_text(json.dumps(["acme/registered"]))
	monkeypatch.setenv("WORKFLOW_HEAL_CONSUMER_REGISTRY", str(registry))
	issue = _issue(body=_issue()["body"].replace(REPO, "acme/registered"))
	fake = FakeGh()
	fake.routes["repos/acme/registered/actions/runs/111/jobs?per_page=100"] = fake.routes[f"repos/{REPO}/actions/runs/111/jobs?per_page=100"]
	fake.routes["repos/acme/registered/actions/jobs/11/logs"] = _review_log()
	fake.routes["repos/acme/registered/actions/runs/111/artifacts?per_page=100"] = {"artifacts": []}
	fake.routes[f"repos/acme/registered/actions/runs?head_sha={HEAD_SHA}&per_page=30"] = {"workflow_runs": [
		{"id": 111, "repository": {"full_name": "acme/registered"}, "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144"},
	]}
	_collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert "repos/acme/registered/actions/jobs/11/logs" in fake.paths


def test_heal_context_reads_the_intake_fields() -> None:
	ctx = ev.heal_context(_issue())
	assert ctx["source_repo"] == REPO and ctx["source_number"] == "6133"
	assert ctx["head_sha"] == HEAD_SHA and ctx["target_branch"] == "main"
	assert ctx["failure_reason"] == "editor_changes_lost"


def test_select_jobs_falls_back_to_the_review_job_when_nothing_failed() -> None:
	jobs = [
		{"id": 1, "name": "review / gate", "conclusion": "success"},
		{"id": 2, "name": "review / codex-agent", "conclusion": "success"},
		{"id": 3, "name": "lint", "conclusion": "success"},
	]
	assert [job["id"] for job in ev.select_jobs(jobs, 3)] == [2]
	jobs[2]["conclusion"] = "failure"
	assert [job["id"] for job in ev.select_jobs(jobs, 3)] == [3]
	assert ev.select_jobs([{"id": 9, "name": "codex-agent", "conclusion": "skipped"}], 3)[0]["id"] == 9
	assert ev.select_jobs([{"id": 9, "name": "build", "conclusion": "success"}], 3) == []
	assert [job["id"] for job in ev.select_jobs([{"id": 10, "name": "review / codex-agent (claude-branch-review)", "conclusion": "success"}], 3)] == [10]
	assert ev.select_jobs([{"id": 11, "name": "review / codex-agent-other", "conclusion": "success"}], 3) == []


def test_lenient_json_reads_paginated_concatenated_arrays(tmp_path: Path) -> None:
	path = tmp_path / "comments.json"
	path.write_text('[{"id": 1}]\n[{"id": 2}]\n')
	assert ev._load_json_lenient(str(path)) == [{"id": 1}, {"id": 2}]
	assert ev._load_json_lenient(str(tmp_path / "missing.json")) is None


def test_lenient_json_rejects_excessive_nesting(tmp_path: Path) -> None:
	path = tmp_path / "comments.json"
	path.write_text("[" * 1100)
	assert ev._load_json_lenient(str(path)) is None


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def _zip(members: dict[str, bytes]) -> bytes:
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w") as archive:
		for name, data in members.items():
			archive.writestr(name, data)
	return buffer.getvalue()


def test_written_evidence_redacts_credential_shaped_strings(tmp_path: Path) -> None:
	text = (
		"token ghp_" + "a" * 36 + " and github_pat_" + "b" * 30 + "\n"
		"OPENROUTER sk-or-v1-" + "c" * 40 + " slack xoxb-" + "1" * 20 + " aws AKIA" + "D" * 16 + "\n"
		"Authorization: Bearer abcdefghijklmnop\nkeep this line\n"
	)
	out = ev.redact_secrets(text)
	assert "ghp_" not in out and "github_pat_" not in out and "sk-or-v1" not in out
	assert "xoxb-" not in out and "AKIA" not in out and "abcdefghijklmnop" not in out
	assert "Authorization: Bearer [REDACTED]" in out and "keep this line" in out
	collector = _collector(tmp_path, FakeGh())
	collector._write("x.txt", text)
	assert "ghp_" not in (tmp_path / "evidence" / "x.txt").read_text()
	collector._write_json("meta.json", {"title": text})
	assert "ghp_" not in (tmp_path / "evidence" / "meta.json").read_text()
	assert "[REDACTED]" in (tmp_path / "evidence" / "meta.json").read_text()
	assert "ghp_" not in ev._safe_name("ghp_" + "z" * 36)
	(tmp_path / "evidence" / "INDEX.md").write_text(text)
	assert "ghp_" not in ev.render_prompt_section(str(tmp_path / "evidence"))


def test_collection_redacts_metadata_in_index_and_manifest(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes[f"repos/{REPO}/actions/runs/111/jobs?per_page=100"]["jobs"][0]["workflow_name"] = "ghp_" + "q" * 36
	fake.routes[f"repos/{REPO}/issues?labels=ai:workflow-heal&state=all&per_page=100&page=1"][1]["title"] = "ghp_" + "t" * 36
	_collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	for rel in ("INDEX.md", "lineage.json", "manifest.json", f"runs/{REPO.replace('/', '__')}__111/meta.json"):
		assert "ghp_" not in (tmp_path / "evidence" / rel).read_text()


def test_reviewer_logs_keep_only_status_and_stderr_members() -> None:
	blob = _zip({"status_deepseek.txt": b"rc=0", "review_deepseek.txt": b"long review", "pass1_x.log": b"wire", "slot.err": b"boom"})
	kept = dict(ev.extract_artifact_texts(blob, max_file_bytes=1000, member_re=ev.ARTIFACT_MEMBER_RES["reviewer-logs"]))
	assert set(kept) == {"status_deepseek.txt", "slot.err"}
	assert ev._artifact_kind("reviewer-logs-37150557820-1") == "reviewer-logs"
	assert ev._artifact_kind("codex-review-autofix-failure-logs-1-2") == "codex-review-autofix-failure-logs"
	assert ev._artifact_kind("other-1-2") == ""


def test_artifact_extraction_is_allowlisted_and_traversal_safe() -> None:
	blob = _zip({
		"previous_reviews/editor_attempt_1.err": b"editor stderr\x1b[31m red\n",
		"../escape.txt": b"nope",
		"/abs.txt": b"nope",
		"binary.bin": b"\x00\x01",
		"summariser_pass1.log": b"x" * 5000,
	})
	out = dict(ev.extract_artifact_texts(blob, max_file_bytes=1000))
	assert set(out) == {"previous_reviews__editor_attempt_1.err", "summariser_pass1.log"}
	assert "\x1b" not in out["previous_reviews__editor_attempt_1.err"]
	assert len(out["summariser_pass1.log"].encode()) <= 1000
	assert ev.extract_artifact_texts(b"not a zip", max_file_bytes=1000) == []


def test_artifact_extraction_limits_cumulative_decompression(monkeypatch) -> None:
	monkeypatch.setattr(ev, "MAX_ARTIFACT_BYTES", 8)
	blob = _zip({"a.err": b"a" * 6, "b.err": b"b" * 6})
	assert dict(ev.extract_artifact_texts(blob, max_file_bytes=100)) == {"a.err": "a" * 6}


def test_index_does_not_report_unmerged_prior_fix_as_merged() -> None:
	index = ev.render_index(
		display_root="/evidence", issue_number=1, ctx={}, runs=[], provenance={},
		lineage=[{"number": 2, "prs": [{"number": 3, "state": "CLOSED", "merged": False, "base": "main"}]}],
		timeline=[], environment={}, skipped=[], api_calls=0, default_branch="main",
	)
	assert "PR #3 CLOSED, targeting `main` (not merged)" in index
	assert "PR #3 CLOSED, merged into" not in index


# ---------------------------------------------------------------------------
# Collector end to end against a fake `gh`
# ---------------------------------------------------------------------------


class FakeGh:
	def __init__(self, *, remaining: int = 4000, missing: set[str] | None = None) -> None:
		self.remaining = remaining
		self.missing = missing or set()
		self.paths: list[str] = []
		self.queries: list[str] = []
		self.provenance_comment_nodes: dict[int, dict | None] = {}
		jobs = [
			{"id": 10, "name": "review / gate", "conclusion": "success", "status": "completed", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "workflow_name": "Internal: AI Review & Autofix", "steps": []},
			{"id": 11, "name": "review / codex-agent", "conclusion": "success", "status": "completed", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "workflow_name": "Internal: AI Review & Autofix", "steps": STEPS},
		]
		self.routes = {
			"user": {"login": "healer"},
			"rate_limit": {"resources": {"core": {"limit": 5000, "remaining": remaining, "reset": 1}, "graphql": {"limit": 5000, "remaining": 4999, "reset": 1}}},
			"provenance": {"data": {"issue": {"issue": {"author": {"login": "healer"}, "lastEditedAt": None, "editor": None, "userContentEdits": {"totalCount": 0, "nodes": []}}}, "comments": []}},
			f"repos/{REPO}/actions/runs/111/jobs?per_page=100": {"jobs": jobs},
			f"repos/{REPO}/actions/runs/222/jobs?per_page=100": {"jobs": jobs},
			f"repos/{REPO}/actions/jobs/11/logs": _review_log(),
			f"repos/{REPO}/actions/runs/111/artifacts?per_page=100": {"artifacts": [
				{"id": 501, "name": "codex-review-autofix-failure-logs-111-1", "expired": False, "size_in_bytes": 100},
				{"id": 502, "name": "some-other-artifact", "expired": False, "size_in_bytes": 100},
			]},
			f"repos/{REPO}/actions/runs/222/artifacts?per_page=100": {"artifacts": []},
			f"repos/{REPO}/actions/artifacts/501/zip": _zip({"editor_attempt_1.err": b"Review isolation snapshot or transfer rejected\n"}),
			f"repos/{REPO}/pulls/6133": {"number": 6133, "state": "open", "merged_at": None, "base": {"ref": "main"}, "head": {"ref": "ai/issue-5144", "sha": HEAD_SHA}},
			f"repos/{REPO}/compare/{HEAD_SHA}...main": {"status": "ahead", "ahead_by": 2, "commits": [{"sha": "c" * 40, "commit": {"message": "fix things"}}], "files": [{"filename": "scripts/x.sh"}]},
			f"repos/{REPO}/issues?labels=ai:workflow-heal&state=all&per_page=100&page=1": [
				_issue(),
				dict(_issue(), number=4477, title="earlier heal", state="closed", state_reason="completed"),
				dict(_issue(), number=4000, title="unrelated", body="<!-- workflow-failure-heal:fp=" + "e" * 64 + " -->"),
			],
			"graphql": {"data": {"repository": {"i4477": {"closedByPullRequestsReferences": {"nodes": [
				{"number": 4478, "state": "MERGED", "merged": True, "mergedAt": "2026-09-25T16:32:08Z", "baseRefName": "orchestrator/project-4139", "mergeCommit": {"oid": MERGE_SHA}},
			]}}}}},
			f"repos/{REPO}/compare/main...{MERGE_SHA}": {"status": "diverged", "ahead_by": 45, "behind_by": 252},
			f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30": {"workflow_runs": [
				{"id": 111, "name": "Internal: AI Review & Autofix [pr:6133]", "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "created_at": "2026-10-04T00:47:35Z", "html_url": f"https://github.com/{REPO}/actions/runs/111"},
			]},
		}

	def __call__(self, cmd, capture_output=True, timeout=None, check=False):
		assert cmd[:2] == ["gh", "api"]
		args = [arg for arg in cmd[2:] if arg != "--allow-escape-sequences"]
		key = ("provenance" if "userContentEdits" in args[-1] else "graphql") if args[0] == "graphql" else args[-1]
		self.paths.append(key)
		if args[0] == "graphql":
			self.queries.append(args[-1])
		if key in self.missing or key not in self.routes:
			return subprocess.CompletedProcess(cmd, 1, b"", b"gh: Not Found (HTTP 404)")
		value = self.routes[key]
		if key == "provenance":
			value = json.loads(json.dumps(value))
			value["data"]["comments"] = [
				self.provenance_comment_nodes.get(int(number), {"databaseId": int(number), "author": {"login": "healer"}, "lastEditedAt": None, "editor": None, "userContentEdits": {"totalCount": 0, "nodes": []}})
				for number in re.findall(r'"IC_(\d+)"', args[-1])
			]
		data = value if isinstance(value, bytes) else (value.encode() if isinstance(value, str) else json.dumps(value).encode())
		return subprocess.CompletedProcess(cmd, 0, data, b"")


def _collector(tmp_path: Path, fake: FakeGh, **kwargs):
	return ev.Collector(
		ev.GitHub(fake, sleep=lambda _s: None),
		tmp_path / "evidence",
		display_root="/evidence",
		openrouter_status=lambda: {"available": True, "limit": 10, "usage": 9.5, "limit_remaining": 0.5},
		**kwargs,
	)


def _occurrence(run_id: int) -> dict:
	return {"id": run_id, "node_id": f"IC_{run_id}", "user": {"login": "healer"}, "body": f"<!-- workflow-failure-heal:occurrence -->\n- **Source pull request:** https://github.com/{REPO}/pull/6133 ({REPO}#6133)\n- **Failed on branch:** `ai/issue-5144`\n- **Head SHA:** `{HEAD_SHA}`\n- **Failed run:** https://github.com/{REPO}/actions/runs/{run_id}\n"}


def test_collect_builds_the_bundle_and_index(tmp_path: Path) -> None:
	fake = FakeGh()
	manifest = _collector(tmp_path, fake).collect(_issue(), [_occurrence(222)], issue_repo=REPO)
	out = tmp_path / "evidence"
	job_file = out / f"runs/{REPO.replace('/', '__')}__111/job-11.txt"
	assert job_file.is_file()
	job_text = job_file.read_text()
	assert job_text.startswith('# Job "review / codex-agent" (conclusion: success)')
	assert "UNTRUSTED" in job_text and "GIT_WORK_TREE" in job_text
	artifact = out / f"runs/{REPO.replace('/', '__')}__111/artifact-codex-review-autofix-failure-logs-111-1/editor_attempt_1.err"
	assert "transfer rejected" in artifact.read_text()
	# Only the allowlisted artifact is downloaded.
	assert f"repos/{REPO}/actions/artifacts/502/zip" not in fake.paths
	index = (out / "INDEX.md").read_text()
	assert "UNTRUSTED DATA" in index and "`/evidence/runs/" in index
	assert "PR #4478 MERGED, merged into `orchestrator/project-4139` at bbbbbbbbbbbb; reached `main`: NO" in index
	assert "#4000" not in index  # different lineage
	assert "Source PR acme/coding-workflows#6133: state open" in index
	assert "`main` vs the failing head" in index
	assert "OpenRouter key: limit 10, usage 9.5, remaining 0.5" in index
	assert "GitHub API core: 4000/5000 remaining" in index
	data = json.loads((out / "manifest.json").read_text())
	assert data == manifest and data["schema"] == "workflow_failure_heal_evidence.v1"
	assert {"path": "INDEX.md", "bytes": (out / "INDEX.md").stat().st_size} in data["files"]
	assert all(not item["path"].startswith("cache/") for item in data["files"])
	# Budget: jobs, log, artifact list + 1 download per run with artifacts,
	# PR, compare, issue list, GraphQL, lineage compare, timeline.
	assert manifest["api_calls"] == len([p for p in fake.paths if p != "rate_limit"])
	assert manifest["api_calls"] <= 20


def test_lineage_excludes_current_issue_when_its_number_is_a_string(tmp_path: Path) -> None:
	_collector(tmp_path, FakeGh()).collect(_issue(number="7000"), [], issue_repo=REPO)
	lineage = json.loads((tmp_path / "evidence" / "lineage.json").read_text())
	assert [item["number"] for item in lineage] == [4477]
	assert "#7000 gen" not in (tmp_path / "evidence" / "INDEX.md").read_text()


def test_lineage_skips_malformed_issue_numbers(tmp_path: Path) -> None:
	fake = FakeGh()
	issue_list = fake.routes[f"repos/{REPO}/issues?labels=ai:workflow-heal&state=all&per_page=100&page=1"]
	issue_list.append(dict(_issue(), number=None))
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert manifest["files"]
	assert [item["number"] for item in json.loads((tmp_path / "evidence" / "lineage.json").read_text())] == [4477]


def test_lineage_deduplicates_issues_across_pages(tmp_path: Path) -> None:
	fake = FakeGh()
	first_page = fake.routes[f"repos/{REPO}/issues?labels=ai:workflow-heal&state=all&per_page=100&page=1"]
	first_page.extend({"number": n, "author_association": "NONE"} for n in range(1000, 1097))
	fake.routes[f"repos/{REPO}/issues?labels=ai:workflow-heal&state=all&per_page=100&page=2"] = [dict(first_page[1])]
	_collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	lineage = json.loads((tmp_path / "evidence" / "lineage.json").read_text())
	assert [item["number"] for item in lineage] == [4477]
	assert len([path for path in fake.paths if path == "graphql"]) == 1


def test_artifact_download_limit_records_skipped_artifacts(tmp_path: Path) -> None:
	fake = FakeGh()
	artifacts = fake.routes[f"repos/{REPO}/actions/runs/111/artifacts?per_page=100"]["artifacts"]
	for artifact_id in (503, 504):
		artifacts.append({"id": artifact_id, "name": f"reviewer-logs-{artifact_id}-1", "expired": False, "size_in_bytes": 100})
		fake.routes[f"repos/{REPO}/actions/artifacts/{artifact_id}/zip"] = _zip({"status_x.txt": b"ok"})
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": "artifact:reviewer-logs-504-1", "reason": "download_limit"} in manifest["skipped"]
	assert f"repos/{REPO}/actions/artifacts/504/zip" not in fake.paths


def test_a_later_stage_reuses_completed_runs(tmp_path: Path) -> None:
	_collector(tmp_path, FakeGh()).collect(_issue(), [_occurrence(222)], issue_repo=REPO)
	second = FakeGh()
	manifest = _collector(tmp_path, second).collect(_issue(), [_occurrence(222)], issue_repo=REPO)
	assert not any("/actions/runs/111/" in path or "/actions/jobs/" in path for path in second.paths)
	assert manifest["api_calls"] <= 8
	assert "reused from an earlier stage" in (tmp_path / "evidence" / "INDEX.md").read_text()


def test_issue_edited_by_another_account_drops_cross_repo_reads(tmp_path: Path, monkeypatch) -> None:
	monkeypatch.setenv("WORKFLOW_HEAL_CONSUMER_REGISTRY", str(tmp_path / "consumers.json"))
	(tmp_path / "consumers.json").write_text(json.dumps(["acme/registered"]))
	issue = _issue(body=_issue()["body"].replace(REPO, "acme/registered"))
	fake = FakeGh()
	fake.routes["provenance"]["data"]["issue"]["issue"].update({
		"lastEditedAt": "2026-10-04T12:00:00Z", "editor": {"login": "intruder"},
		"userContentEdits": {"totalCount": 1, "nodes": [{"editor": {"login": "intruder"}}]},
	})
	manifest = _collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert manifest["run_provenance"] == {"status": "unverified", "reason": "issue_edited_by_other"}
	assert {"part": "run_provenance", "reason": "issue_edited_by_other"} in manifest["skipped"]
	assert {"part": "source_repo", "reason": "provenance_unverified"} in manifest["skipped"]
	assert not any("repos/acme/registered/" in path for path in fake.paths)
	assert not any("/actions/jobs/" in path for path in fake.paths)


def test_untrusted_issue_author_skips_graphql_and_all_runs(tmp_path: Path) -> None:
	fake = FakeGh()
	manifest = _collector(tmp_path, fake).collect(_issue(user={"login": "other", "type": "User"}), [_occurrence(222)], issue_repo=REPO)
	assert manifest["run_provenance"]["reason"] == "issue_not_authored_by_intake"
	assert "provenance" not in fake.paths and not any("/actions/jobs/" in path for path in fake.paths)


def test_unavailable_intake_identity_skips_all_runs(tmp_path: Path) -> None:
	fake = FakeGh(missing={"user"})
	manifest = _collector(tmp_path, fake).collect(_issue(), [_occurrence(222)], issue_repo=REPO)
	assert manifest["run_provenance"]["reason"] == "intake_identity_unavailable"
	assert "provenance" not in fake.paths and not any("/actions/jobs/" in path for path in fake.paths)


def test_comment_edited_by_other_does_not_suppress_body_run(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.provenance_comment_nodes[222] = {
		"databaseId": 222, "author": {"login": "healer"}, "lastEditedAt": "2026-10-04T12:00:00Z",
		"editor": {"login": "other"}, "userContentEdits": {"totalCount": 1, "nodes": [{"editor": {"login": "other"}}]},
	}
	manifest = _collector(tmp_path, fake).collect(_issue(), [_occurrence(222)], issue_repo=REPO)
	assert manifest["run_provenance"]["status"] == "verified"
	assert f"repos/{REPO}/actions/runs/111/jobs?per_page=100" in fake.paths
	assert f"repos/{REPO}/actions/runs/222/jobs?per_page=100" not in fake.paths


@pytest.mark.parametrize("edits", [
	{"totalCount": 1, "nodes": [{"editor": None}]},
	{"totalCount": 101, "nodes": [{"editor": {"login": "healer"}}]},
	{"totalCount": 0, "nodes": None},
])
def test_incomplete_or_unattributed_edit_history_fails_closed(tmp_path: Path, edits: dict) -> None:
	fake = FakeGh()
	fake.routes["provenance"]["data"]["issue"]["issue"]["userContentEdits"] = edits
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert manifest["run_provenance"]["status"] == "unverified"
	assert not any("/actions/jobs/" in path for path in fake.paths)


def test_graphql_errors_fail_closed_even_with_partial_data(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes["provenance"]["errors"] = [{"message": "unavailable"}]
	manifest = _collector(tmp_path, fake).collect(_issue(), [_occurrence(222)], issue_repo=REPO)
	assert manifest["run_provenance"]["status"] == "unverified"
	assert not any("/actions/jobs/" in path for path in fake.paths)


def test_unrelated_consumer_run_skipped_but_valid_one_collected(tmp_path: Path, monkeypatch) -> None:
	monkeypatch.setenv("WORKFLOW_HEAL_CONSUMER_REGISTRY", str(tmp_path / "consumers.json"))
	(tmp_path / "consumers.json").write_text(json.dumps(["acme/registered"]))
	issue = _issue(body=_issue()["body"].replace(REPO, "acme/registered"))
	fake = FakeGh()
	fake.routes[f"repos/acme/registered/actions/runs?head_sha={HEAD_SHA}&per_page=30"] = {"workflow_runs": [
		{"id": 111, "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "repository": {"full_name": "acme/registered"}},
	]}
	fake.routes["repos/acme/registered/actions/runs/222"] = {"id": 222, "head_sha": MERGE_SHA, "head_branch": "other", "pull_requests": [], "repository": {"full_name": "acme/registered"}}
	fake.routes["repos/acme/registered/actions/runs/111/jobs?per_page=100"] = fake.routes[f"repos/{REPO}/actions/runs/111/jobs?per_page=100"]
	fake.routes["repos/acme/registered/actions/jobs/11/logs"] = _review_log()
	fake.routes["repos/acme/registered/actions/runs/111/artifacts?per_page=100"] = {"artifacts": []}
	comment = _occurrence(222)
	comment["body"] = comment["body"].replace(REPO, "acme/registered")
	manifest = _collector(tmp_path, fake).collect(issue, [comment], issue_repo=REPO)
	assert {"part": "run:acme/registered:222", "reason": "unverified_run_head_mismatch"} in manifest["skipped"]
	assert "repos/acme/registered/actions/runs/222/jobs?per_page=100" not in fake.paths
	assert "repos/acme/registered/actions/runs/111/jobs?per_page=100" in fake.paths
	assert "repos/acme/registered/actions/runs/111" not in fake.paths  # timeline hit


def test_default_branch_review_run_is_accepted_with_pr_token(tmp_path: Path, monkeypatch) -> None:
	monkeypatch.setenv("WORKFLOW_HEAL_CONSUMER_REGISTRY", str(tmp_path / "consumers.json"))
	(tmp_path / "consumers.json").write_text(json.dumps(["acme/registered"]))
	issue = _issue(body=_issue()["body"].replace(REPO, "acme/registered"))
	fake = FakeGh()
	fake.routes["repos/acme/registered/actions/runs/111"] = {
		"id": 111, "head_sha": MERGE_SHA, "head_branch": "main", "name": "AI Review [pr:6133]", "repository": {"full_name": "acme/registered"},
	}
	fake.routes["repos/acme/registered/actions/runs/111/jobs?per_page=100"] = fake.routes[f"repos/{REPO}/actions/runs/111/jobs?per_page=100"]
	fake.routes["repos/acme/registered/actions/jobs/11/logs"] = _review_log()
	fake.routes["repos/acme/registered/actions/runs/111/artifacts?per_page=100"] = {"artifacts": []}
	_collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert "repos/acme/registered/actions/runs/111" in fake.paths
	assert "repos/acme/registered/actions/runs/111/jobs?per_page=100" in fake.paths


def test_run_metadata_rejects_different_repository(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes["repos/acme/registered/actions/runs/111"] = {
		"id": 111, "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "repository": {"full_name": "other/repo"},
	}
	ref = {"repo": "acme/registered", "run_id": "111", "source_repo": "acme/registered", "source_number": "6133", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144"}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_repo_mismatch")


def test_invalid_comment_node_id_is_never_queried(tmp_path: Path) -> None:
	fake = FakeGh()
	comment = _occurrence(222)
	comment["node_id"] = 'bad" } gh api'
	_collector(tmp_path, fake).collect(_issue(), [comment], issue_repo=REPO)
	assert all('bad" } gh api' not in query for query in fake.queries)
	assert f"repos/{REPO}/actions/runs/222/jobs?per_page=100" not in fake.paths


def test_unverified_cached_run_is_removed(tmp_path: Path, monkeypatch) -> None:
	monkeypatch.setenv("WORKFLOW_HEAL_CONSUMER_REGISTRY", str(tmp_path / "consumers.json"))
	(tmp_path / "consumers.json").write_text(json.dumps(["acme/registered"]))
	stale = tmp_path / "evidence/runs/acme__registered__111"
	stale.mkdir(parents=True)
	(stale / "meta.json").write_text('{"complete": true}')
	issue = _issue(body=_issue()["body"].replace(REPO, "acme/registered"))
	fake = FakeGh()
	fake.routes["repos/acme/registered/actions/runs/111"] = {"id": 111, "head_sha": MERGE_SHA, "pull_requests": [], "repository": {"full_name": "acme/registered"}}
	_collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert not stale.exists()


def test_legacy_run_refs_without_verification_arguments_keep_order_and_keys() -> None:
	refs = ev.trusted_run_refs(_issue(), [_occurrence(222)], allowed_repos=[REPO], limit=2)
	assert [(ref["repo"], ref["run_id"], ref["url"]) for ref in refs] == [
		(REPO, "111", f"https://github.com/{REPO}/actions/runs/111"),
		(REPO, "222", f"https://github.com/{REPO}/actions/runs/222"),
	]
	assert refs[-1]["origin"] == "comment:222" and refs[-1]["head_sha"] == HEAD_SHA


def test_occurrence_uses_its_own_head_not_the_original_issue_head() -> None:
	comment = _occurrence(222)
	comment["body"] = comment["body"].replace(HEAD_SHA, MERGE_SHA)
	refs = ev.trusted_run_refs(_issue(), [comment], allowed_repos=[REPO], limit=3, verified_comment_ids={222})
	assert refs[-1]["head_sha"] == MERGE_SHA
	assert refs[-1]["source_repo"] == REPO and refs[-1]["source_number"] == "6133"
	assert refs[0]["head_sha"] == HEAD_SHA


def test_a_failed_log_fetch_is_retried_by_the_next_stage(tmp_path: Path) -> None:
	first = FakeGh(missing={f"repos/{REPO}/actions/jobs/11/logs"})
	manifest = _collector(tmp_path, first).collect(_issue(), [], issue_repo=REPO)
	assert {"part": "job:11", "reason": "log_unavailable"} in manifest["skipped"]
	second = FakeGh()
	_collector(tmp_path, second).collect(_issue(), [], issue_repo=REPO)
	assert f"repos/{REPO}/actions/jobs/11/logs" in second.paths


def test_low_rate_limit_skips_the_optional_parts(tmp_path: Path) -> None:
	fake = FakeGh(remaining=100)
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	reasons = {(item["part"], item["reason"]) for item in manifest["skipped"]}
	assert ("artifacts:111", "rate_limit_low") in reasons
	assert ("timeline", "rate_limit_low") in reasons
	assert not any(path.endswith("/zip") or "actions/runs?head_sha" in path for path in fake.paths)
	assert manifest["rate_limit_low"] is True
	# The job log is still fetched: it is the core of the bundle.
	assert f"repos/{REPO}/actions/jobs/11/logs" in fake.paths


def test_total_size_limit_drops_oldest_extras_first(tmp_path: Path) -> None:
	manifest = _collector(tmp_path, FakeGh(), max_total_bytes=12_000, max_file_bytes=6000).collect(_issue(), [_occurrence(222)], issue_repo=REPO)
	out = tmp_path / "evidence"
	total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file() and "cache" not in p.parts and p.name not in ("manifest.json", "INDEX.md"))
	assert total <= 12_000
	size_skips = [item for item in manifest["skipped"] if item["reason"] == "total_size_limit"]
	assert len(size_skips) == 1 and size_skips[0]["part"].startswith("files:")
	run_dir = f"runs/{REPO.replace('/', '__')}"
	# Artifacts go before job logs, and the oldest run's job log before the newest's.
	assert not (out / f"{run_dir}__111/artifact-codex-review-autofix-failure-logs-111-1").exists() or not any((out / f"{run_dir}__111/artifact-codex-review-autofix-failure-logs-111-1").iterdir())
	assert not (out / f"{run_dir}__111/job-11.txt").exists()
	assert (out / f"{run_dir}__222/job-11.txt").exists()
	assert "(dropped: size limit)" in (out / "INDEX.md").read_text()
	index = (out / "INDEX.md").read_text()
	# The index never points at a file the budget removed.
	for line in index.splitlines():
		for path in __import__("re").findall(r"`/evidence/([^`]+)`", line):
			assert (out / path).exists() or path.endswith("/"), path


def test_no_trusted_runs_still_writes_provenance(tmp_path: Path) -> None:
	issue = _issue(body=_issue()["body"].replace(f"<!-- workflow-failure-heal:runs={REPO}:111 -->\n", ""))
	manifest = _collector(tmp_path, FakeGh()).collect(issue, [], issue_repo=REPO)
	assert {"part": "runs", "reason": "no_trusted_run_links"} in manifest["skipped"]
	assert "(no run could be linked; see Skipped)" in (tmp_path / "evidence" / "INDEX.md").read_text()


def test_dropped_runs_are_pruned_from_the_cache(tmp_path: Path) -> None:
	stale = tmp_path / "evidence" / "runs" / "acme__old__1"
	stale.mkdir(parents=True)
	(stale / "meta.json").write_text("{}")
	_collector(tmp_path, FakeGh()).collect(_issue(), [], issue_repo=REPO)
	assert not stale.exists()


# ---------------------------------------------------------------------------
# GitHub wrapper and OpenRouter status
# ---------------------------------------------------------------------------


def test_github_wrapper_retries_transient_errors_only() -> None:
	calls = []

	def runner(cmd, capture_output=True, timeout=None, check=False):
		calls.append(cmd)
		if len(calls) == 1:
			return subprocess.CompletedProcess(cmd, 1, b"", b"HTTP 502: Bad Gateway")
		return subprocess.CompletedProcess(cmd, 0, b'{"ok": true}', b"")

	gh = ev.GitHub(runner, sleep=lambda _s: None)
	assert gh.json("repos/a/b") == {"ok": True}
	assert gh.calls == 2

	def not_found(cmd, capture_output=True, timeout=None, check=False):
		return subprocess.CompletedProcess(cmd, 1, b"", b"HTTP 404: Not Found")

	gh = ev.GitHub(not_found, sleep=lambda _s: None)
	assert gh.json("repos/a/b") is None and gh.calls == 1


def test_job_log_fetch_falls_back_when_gh_lacks_the_escape_flag() -> None:
	seen = []

	def old_gh(cmd, capture_output=True, timeout=None, check=False):
		seen.append(cmd)
		if "--allow-escape-sequences" in cmd:
			return subprocess.CompletedProcess(cmd, 1, b"", b"unknown flag: --allow-escape-sequences")
		return subprocess.CompletedProcess(cmd, 0, b"log", b"")

	gh = ev.GitHub(old_gh, sleep=lambda _s: None)
	assert gh.raw("repos/a/b/actions/jobs/1/logs", escapes=True) == b"log"
	assert gh.raw("repos/a/b/actions/jobs/2/logs", escapes=True) == b"log"
	# The flag is probed once, then dropped for the rest of the run.
	assert sum("--allow-escape-sequences" in cmd for cmd in seen) == 1


def test_openrouter_status_reports_numbers_only() -> None:
	class Response(io.BytesIO):
		def __enter__(self):
			return self

		def __exit__(self, *exc):
			return False

	seen = {}

	def opener(request, timeout=None):
		seen["auth"] = request.get_header("Authorization")
		return Response(json.dumps({"data": {"label": "secret-label", "limit": 10, "usage": 3, "limit_remaining": 7, "is_free_tier": False}}).encode())

	status = ev.openrouter_key_status("sk-test", opener=opener)
	assert status == {"available": True, "limit": 10, "usage": 3, "limit_remaining": 7, "is_free_tier": False}
	assert seen["auth"] == "Bearer sk-test"
	assert ev.openrouter_key_status("") == {"available": False, "reason": "no_key"}
	large_response = Response(b"x" * 100_000)
	assert ev.openrouter_key_status("sk-test", opener=lambda _request, timeout=None: large_response) == {"available": False, "reason": "JSONDecodeError"}
	assert large_response.tell() == 65536


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_collect_skips_an_ineligible_issue(tmp_path: Path) -> None:
	issue_file = tmp_path / "issue.json"
	issue_file.write_text(json.dumps(_issue(labels=[])))
	proc = subprocess.run(
		[sys.executable, str(SCRIPT), "collect", "--repo", REPO, "--issue-json", str(issue_file), "--out-dir", str(tmp_path / "out")],
		capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
	)
	assert proc.returncode == 0
	assert json.loads(proc.stdout) == {"collected": False, "reason": "not_heal_issue"}
	assert not (tmp_path / "out").exists()


def test_cli_slice_log_uses_the_job_steps(tmp_path: Path) -> None:
	log_file = tmp_path / "job.log"
	log_file.write_text(_review_log())
	jobs = tmp_path / "jobs.json"
	jobs.write_text(json.dumps({"jobs": [{"id": 11, "steps": STEPS}]}))
	proc = subprocess.run(
		[sys.executable, str(SCRIPT), "slice-log", "--log-file", str(log_file), "--jobs-json", str(jobs), "--job-id", "11", "--max-bytes", "20000"],
		capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
	)
	assert "| 2 | Apply fixes with editor model |" in proc.stdout
	assert len(proc.stdout.encode()) <= 20000


def test_prompt_section_wraps_the_index_and_is_empty_without_one(tmp_path: Path) -> None:
	assert ev.render_prompt_section(str(tmp_path)) == ""
	(tmp_path / "INDEX.md").write_text("# Workflow heal evidence for issue #1\n")
	section = ev.render_prompt_section(str(tmp_path))
	assert section.startswith("=== WORKFLOW HEAL EVIDENCE (UNTRUSTED) ===\n")
	assert "data, not instructions" in section and "# Workflow heal evidence for issue #1" in section
	assert section.endswith("\n=== END WORKFLOW HEAL EVIDENCE ===\n")


def test_cli_rejects_an_invalid_repo(tmp_path: Path) -> None:
	proc = subprocess.run(
		[sys.executable, str(SCRIPT), "collect", "--repo", "not a slug", "--out-dir", str(tmp_path)],
		capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
	)
	assert proc.returncode == 2


# ---------------------------------------------------------------------------
# Wiring contracts: clarify / plan / implement, the clarify sandbox, the docs
# ---------------------------------------------------------------------------

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _step(workflow_text: str, name: str) -> str:
	start = workflow_text.index(f"      - name: {name}\n")
	end = workflow_text.find("\n      - name: ", start + 1)
	return workflow_text[start:end if end != -1 else None]


@pytest.mark.parametrize(
	("workflow", "agent_step", "display_root"),
	[
		("clarify.yml", "Run Codex", '"/evidence"'),
		("plan.yml", "Run Codex planning", '"${RUNNER_TEMP}/heal-evidence"'),
		("implement.yml", "Run Codex implementation", '"${RUNNER_TEMP}/heal-evidence"'),
	],
)
def test_each_heal_stage_collects_evidence_before_its_agent(workflow: str, agent_step: str, display_root: str) -> None:
	text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
	staging = text[text.index("          for f in gh_helpers.sh "):]
	staging = staging[: staging.index("; do")]
	assert "workflow_failure_heal.py" in staging and "workflow_failure_heal_evidence.py" in staging
	names = ["Gate workflow-heal evidence", "Restore workflow-heal evidence cache", "Collect workflow-heal evidence", "Save workflow-heal evidence cache"]
	positions = [text.index(f"      - name: {name}\n") for name in names]
	assert positions == sorted(positions) and positions[-1] < text.index(f"      - name: {agent_step}\n")
	for name in names:
		step = _step(text, name)
		# Fail open: a broken evidence step never blocks the stage.
		assert "continue-on-error: true" in step, (workflow, name)
	collect = _step(text, "Collect workflow-heal evidence")
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in collect
	assert f"--display-root {display_root}" in collect
	assert '--issue-json "${ISSUE_META_FILE}"' in collect and "--comments-json" in collect
	assert 'echo "HEAL_EVIDENCE_DIR=${evidence_dir}" >> "$GITHUB_ENV"' in collect
	assert "uses: actions/cache/restore@v4" in _step(text, "Restore workflow-heal evidence cache")
	assert "uses: actions/cache/save@v4" in _step(text, "Save workflow-heal evidence cache")
	assert "workflow_failure_heal_evidence.py eligible" in _step(text, "Gate workflow-heal evidence")
	assert len(text.encode("utf-8")) < 480_000  # CLAUDE.md §27


def test_clarify_fetches_recent_occurrences_without_changing_its_prompt_window() -> None:
	text = (WORKFLOWS / "clarify.yml").read_text(encoding="utf-8")
	fetch = _step(text, "Fetch issue comments")
	assert "comments?sort=created&direction=asc&per_page=100" in fetch
	assert "${ISSUE_ALL_COMMENTS_FILE}" in fetch and "jq '.[0:50]'" in fetch
	collect = _step(text, "Collect workflow-heal evidence")
	assert 'evidence_comments_file="${ISSUE_ALL_COMMENTS_FILE:-${ISSUE_COMMENTS_FILE:-}}"' in collect
	assert 'evidence_comments_file="${ISSUE_COMMENTS_FILE:-}"' in collect
	assert "gh api" not in collect
	assert '--comments-json "${evidence_comments_file}"' in collect


def test_prompts_append_the_evidence_section() -> None:
	call = 'workflow_failure_heal_evidence.py prompt-section --evidence-dir "${HEAL_EVIDENCE_DIR}"'
	clarify = _step((WORKFLOWS / "clarify.yml").read_text(encoding="utf-8"), "Run Codex")
	assert call in clarify and 'export CLARIFY_EVIDENCE_DIR="${HEAL_EVIDENCE_DIR:-}"' in clarify
	assert clarify.index('cat "${ISSUE_CONTEXT_FILE}"') < clarify.index(call) < clarify.index('} > "${CODEX_PROMPT_FILE}"')
	implement = _step((WORKFLOWS / "implement.yml").read_text(encoding="utf-8"), "Run Codex implementation")
	assert implement.index('cat "${IMPLEMENTATION_CONTEXT_FILE}"') < implement.index(call)
	plan = (REPO_ROOT / "scripts" / "run_plan_codex.sh").read_text(encoding="utf-8")
	assert plan.index('cat "${PLANNING_CONTEXT_FILE}"') < plan.index(call) < plan.index('} > "${CODEX_PROMPT_FILE}"')


def _clarify_copy_snippet() -> str:
	text = (REPO_ROOT / "scripts" / "clarify_isolated_run.sh").read_text(encoding="utf-8")
	return text[text.index("<<'EVIDENCE_PY'\n") + len("<<'EVIDENCE_PY'\n"): text.index("\nEVIDENCE_PY\n")]


def test_clarify_sandbox_mounts_a_read_only_evidence_copy() -> None:
	text = (REPO_ROOT / "scripts" / "clarify_isolated_run.sh").read_text(encoding="utf-8")
	assert 'evidence_mount=(--mount "type=bind,src=${run_root}/evidence,dst=/evidence,readonly")' in text
	assert '"${evidence_mount[@]}" \\' in text
	# Only a directory under RUNNER_TEMP is accepted, and the host path itself
	# is never mounted: the copy in run_root is.
	assert '[[ "${evidence_src}" == "${evidence_root}"/* ]]' in text
	assert "src=${evidence_src}" not in text and "src=${CLARIFY_EVIDENCE_DIR}" not in text


def test_clarify_evidence_copy_skips_symlinks_and_the_cache(tmp_path: Path) -> None:
	src = tmp_path / "src"
	(src / "runs" / "r1").mkdir(parents=True)
	(src / "cache").mkdir()
	(src / "INDEX.md").write_text("index")
	(src / "runs" / "r1" / "job.txt").write_text("log")
	(src / "cache" / "contained.json").write_text("{}")
	secret = tmp_path / "secret.txt"
	secret.write_text("private")
	(src / "runs" / "r1" / "link.txt").symlink_to(secret)
	(src / "linkdir").symlink_to(tmp_path)
	dest = tmp_path / "dest"
	dest.mkdir()
	proc = subprocess.run([sys.executable, "-", str(src), str(dest)], input=_clarify_copy_snippet(), text=True, capture_output=True, check=False)
	assert proc.returncode == 0, proc.stderr
	copied = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
	assert copied == ["INDEX.md", "runs/r1/job.txt"]


def test_evidence_log_prefix_is_registered() -> None:
	agents_text = (REPO_ROOT / "agents.md").read_text(encoding="utf-8")
	assert "- `WORKFLOW_HEAL_EVIDENCE`" in agents_text
	assert "LOG_PREFIX.name=WORKFLOW_HEAL_EVIDENCE" in agents_text
	assert ev.LOG_PREFIX == "WORKFLOW_HEAL_EVIDENCE"


def test_ci_runs_the_evidence_tests() -> None:
	ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
	assert "tests/test_workflow_failure_heal_evidence.py" in ci


if __name__ == "__main__":
	raise SystemExit(pytest.main([__file__, "-q"]))
