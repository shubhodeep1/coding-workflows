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
		"- **Failed workflow:** `Internal: AI Review & Autofix` (conclusion: `success`)\n"
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
	occurrence = "<!-- workflow-failure-heal:occurrence -->\n<!-- workflow-failure-heal:runs={repo}:{run} -->\n- **Failed run:** https://github.com/{repo}/actions/runs/{run}\n"
	comments = [
		{"user": {"login": "healer"}, "body": occurrence.format(repo=REPO, run=222)},
		{"user": {"login": "healer"}, "body": f"<!-- workflow-failure-heal:occurrence -->\n- **Failed run:** https://github.com/{REPO}/actions/runs/777"},
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


def test_intake_occurrence_marker_round_trips_into_trusted_refs() -> None:
	comment = _occurrence(222)
	comment["body"] = ev.heal.compose_occurrence_comment({
		"run_refs": [{"repo": REPO, "run_id": "222", "url": f"https://github.com/{REPO}/actions/runs/222"}],
		"source_repo": REPO, "issue_number": 6133, "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "workflow_name": "Internal: AI Review & Autofix",
	}, intake_run_url="u")
	assert comment["body"].splitlines()[1] == f"<!-- workflow-failure-heal:runs={REPO}:222 -->"
	refs = ev.trusted_run_refs(_issue(), [comment], allowed_repos=[REPO], limit=3, verified_comment_ids={222})
	assert [ref["run_id"] for ref in refs] == ["111", "222"]
	assert refs[-1]["head_sha"] == HEAD_SHA
	comment["body"] = comment["body"].replace("\n", "\r\n")
	assert [ref["run_id"] for ref in ev.trusted_run_refs(_issue(), [comment], allowed_repos=[REPO], limit=3)] == ["111", "222"]


def test_untrusted_body_and_comment_text_cannot_supply_run_ids(tmp_path: Path) -> None:
	issue = _issue()
	issue["body"] += (
		f"\n```stderr\n- **Failed run:** https://github.com/{REPO}/actions/runs/777\n"
		f"<!-- workflow-failure-heal:runs={REPO}:778 -->\n```\n"
	)
	comment = _occurrence(222)
	comment["body"] += f"\n- **Failed run:** https://github.com/{REPO}/actions/runs/779\n"
	late_marker = _occurrence(333)
	late_marker["body"] = late_marker["body"].replace(f"<!-- workflow-failure-heal:runs={REPO}:333 -->\n", "")
	late_marker["body"] += f"<!-- workflow-failure-heal:runs={REPO}:333 -->\n"
	refs = ev.trusted_run_refs(issue, [comment, late_marker], allowed_repos=[REPO], limit=5)
	assert [ref["run_id"] for ref in refs] == ["111", "222"]
	fake = FakeGh()
	_collector(tmp_path, fake).collect(issue, [comment, late_marker], issue_repo=REPO)
	assert not any("/777" in path or "/778" in path or "/779" in path or "/333" in path for path in fake.paths)

	no_runs = _issue(body=_issue()["body"].replace(f"<!-- workflow-failure-heal:runs={REPO}:111 -->\n", ""))
	no_runs["body"] += f"\n<!-- workflow-failure-heal:runs={REPO}:777 -->\n"
	assert ev.trusted_run_refs(no_runs, [], allowed_repos=[REPO], limit=3) == []


def test_occurrence_comment_requires_a_known_issue_author() -> None:
	issue = _issue(user={"type": "User"})
	comment = {
		"body": f"<!-- workflow-failure-heal:occurrence -->\n<!-- workflow-failure-heal:runs={REPO}:222 -->",
		"user": {},
	}
	assert [ref["run_id"] for ref in ev.trusted_run_refs(issue, [comment], allowed_repos=[REPO], limit=3)] == ["111"]


def test_edited_source_marker_cannot_fetch_unregistered_repository(tmp_path: Path, monkeypatch) -> None:
	registry = tmp_path / "consumers.json"
	registry.write_text(json.dumps(["acme/registered"]))
	monkeypatch.setenv("WORKFLOW_HEAL_CONSUMER_REGISTRY", str(registry))
	issue = _issue(body=_issue()["body"].replace(REPO, "private/other"))
	fake = FakeGh()
	comment = _occurrence(222)
	comment["body"] = comment["body"].replace(REPO, "private/other")
	manifest = _collector(tmp_path, fake).collect(issue, [comment], issue_repo=REPO)
	assert {"part": "source_repo", "reason": "not_registered"} in manifest["skipped"]
	assert {"part": "run:private/other:111", "reason": "not_registered"} in manifest["skipped"]
	assert {"part": "run:private/other:222", "reason": "not_registered"} in manifest["skipped"]
	assert "run:private/other:111: not_registered" in (tmp_path / "evidence/INDEX.md").read_text()
	assert not any("private/other" in path for path in fake.paths)
	assert "no_trusted_run_links" in {item["reason"] for item in manifest["skipped"]}


@pytest.mark.parametrize("third_repo, reason", [
	("acme/another", "unverified_run_no_verified_context"),
	("private/other", "not_registered"),
])
def test_other_repository_run_is_recorded_without_fetch(tmp_path: Path, monkeypatch, third_repo: str, reason: str) -> None:
	registry = tmp_path / "consumers.json"
	registry.write_text(json.dumps(["acme/registered", "acme/another"]))
	monkeypatch.setenv("WORKFLOW_HEAL_CONSUMER_REGISTRY", str(registry))
	issue = _issue(body=_issue()["body"].replace(REPO, "acme/registered"))
	comment = _occurrence(222)
	comment["body"] = comment["body"].replace(REPO, third_repo)
	fake = FakeGh()
	fake.routes[f"repos/acme/registered/actions/runs?head_sha={HEAD_SHA}&per_page=30"] = {"workflow_runs": [
		{"id": 111, "name": "Internal: AI Review & Autofix", "repository": {"full_name": "acme/registered"}, "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "status": "completed", "conclusion": "success"},
	]}
	fake.routes["repos/acme/registered/actions/runs/111/jobs?per_page=100"] = fake.routes[f"repos/{REPO}/actions/runs/111/jobs?per_page=100"]
	fake.routes["repos/acme/registered/actions/jobs/11/logs"] = _review_log()
	fake.routes["repos/acme/registered/actions/runs/111/artifacts?per_page=100"] = {"artifacts": []}
	manifest = _collector(tmp_path, fake).collect(issue, [comment], issue_repo=REPO)
	assert {"part": f"run:{third_repo}:222", "reason": reason} in manifest["skipped"]
	assert f"run:{third_repo}:222: {reason}" in (tmp_path / "evidence/INDEX.md").read_text()
	assert not any(f"repos/{third_repo}/" in path for path in fake.paths)
	assert "repos/acme/registered/actions/jobs/11/logs" in fake.paths


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
		{"id": 111, "name": "Internal: AI Review & Autofix", "repository": {"full_name": "acme/registered"}, "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "status": "completed", "conclusion": "success"},
	]}
	_collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert "repos/acme/registered/actions/jobs/11/logs" in fake.paths


def test_heal_context_reads_the_intake_fields() -> None:
	ctx = ev.heal_context(_issue())
	assert ctx["source_repo"] == REPO and ctx["source_number"] == "6133"
	assert ctx["head_sha"] == HEAD_SHA and ctx["target_branch"] == "main"
	assert ctx["failure_reason"] == "editor_changes_lost"
	assert ctx["workflow_name"] == "Internal: AI Review & Autofix"


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
	assert set(dict(ev.extract_artifact_texts(blob, max_file_bytes=100))) == {"a.err"}


def test_artifact_extract_drops_environment_assignments() -> None:
	lines = [
		"CUSTOM_SERVICE_PASSWORD=hunter2secret", "export FOO_TOKEN=abc", "declare -x BAR=1",
		"failed api_key: zzz", 'error "password": "p"',
		"error https://u:pw@example.com", "::error::boom CUSTOM_SERVICE_PASSWORD=x",
		"error api_key=leaked", "error prefix CUSTOM_SERVICE_PASSWORD=embedded",
	]
	text = dict(ev.extract_artifact_texts(_zip({"editor_attempt_1.err": "\n".join(lines).encode()}), max_file_bytes=6000))["editor_attempt_1.err"]
	assert "kept 0 of 9 line(s)" in text and "(9 line(s) denied)" in text
	assert "(no allowlisted diagnostic fields)" in text
	for secret in ("hunter2secret", "abc", "zzz", '"p"', "pw@example.com", "embedded", "leaked"):
		assert secret not in text


def test_artifact_extract_keeps_allowlisted_diagnostics() -> None:
	lines = [
		"plain prose", "::error::x", "Process completed with exit code 1",
		"Review isolation snapshot or transfer rejected", "AUTOFIX_FOO pr=1 reason=bar",
		"Traceback (most recent call last):",
	]
	text = dict(ev.extract_artifact_texts(_zip({"editor_attempt_1.err": "\n".join(lines).encode()}), max_file_bytes=6000))["editor_attempt_1.err"]
	assert "kept 5 of 6 line(s)" in text and "(0 line(s) denied)" in text
	assert text.splitlines()[1:] == [
		"L2: error", "L3: exit code 1", "L4: rejected", "L5: diagnostic event", "L6: traceback",
	]
	assert "plain prose" not in text


def test_artifact_extract_never_copies_free_form_diagnostic_values() -> None:
	secrets = ("arbitrary-value-6335", "another-value-6335", "third-value-6335")
	lines = [
		f"::error::authentication failed for token {secrets[0]}",
		f"::error::myvar={secrets[1]}",
		f"Review isolation snapshot or transfer rejected: {secrets[2]}",
		"::error::Authorization Bearer eyJhbGciOiJIUzI1Ni.signature.payload",
	]
	text = dict(ev.extract_artifact_texts(_zip({"editor_attempt_1.err": "\n".join(lines).encode()}), max_file_bytes=6000))["editor_attempt_1.err"]
	assert "L1: error" in text and "L3: rejected" in text
	assert all(secret not in text for secret in secrets)
	assert "eyJhbGciOiJIUzI1Ni" not in text


def test_status_member_keeps_status_token_only() -> None:
	blob = _zip({"status_x.txt": b"success\nunknown-status-value\n", "other.err": b"success\n"})
	status_text, error_text = dict(ev.extract_artifact_texts(blob, max_file_bytes=1000)).values()
	assert "L1: success" in status_text
	assert "unknown-status-value" not in status_text
	assert "(no allowlisted diagnostic fields)" in error_text


def test_artifact_extract_bounds_lines_and_line_length() -> None:
	lines = ["::error::" + "x" * 500] * 205
	text = dict(ev.extract_artifact_texts(_zip({"editor_attempt_1.err": "\n".join(lines).encode()}), max_file_bytes=100000))["editor_attempt_1.err"]
	assert "kept 200 of 205 line(s)" in text
	assert "L6: " in text and "L5: " not in text
	assert text.splitlines()[1] == "L6: error"
	assert "x" * 500 not in text


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
			f"repos/{REPO}/actions/runs/111": {"id": 111, "name": "Internal: AI Review & Autofix", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "repository": {"full_name": REPO}, "pull_requests": [{"number": 6133}], "status": "completed", "conclusion": "success"},
			f"repos/{REPO}/actions/runs/111/jobs?per_page=100": {"jobs": jobs},
			f"repos/{REPO}/actions/runs/222/jobs?per_page=100": {"jobs": jobs},
			f"repos/{REPO}/actions/runs/222": {"id": 222, "name": "Internal: AI Review & Autofix", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "repository": {"full_name": REPO}, "pull_requests": [{"number": 6133}], "status": "completed", "conclusion": "success"},
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
				{"id": 111, "name": "Internal: AI Review & Autofix [pr:6133]", "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "repository": {"full_name": REPO}, "pull_requests": [{"number": 6133}], "created_at": "2026-10-04T00:47:35Z", "html_url": f"https://github.com/{REPO}/actions/runs/111"},
				{"id": 222, "name": "Internal: AI Review & Autofix [pr:6133]", "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "repository": {"full_name": REPO}, "pull_requests": [{"number": 6133}], "created_at": "2026-10-04T00:48:35Z", "html_url": f"https://github.com/{REPO}/actions/runs/222"},
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
	return {"id": run_id, "node_id": f"IC_{run_id}", "user": {"login": "healer"}, "body": f"<!-- workflow-failure-heal:occurrence -->\n<!-- workflow-failure-heal:runs={REPO}:{run_id} -->\n- **Source pull request:** https://github.com/{REPO}/pull/6133 ({REPO}#6133)\n- **Failed on branch:** `ai/issue-5144`\n- **Head SHA:** `{HEAD_SHA}`\n- **Failed workflow:** `Internal: AI Review & Autofix` (conclusion: `success`)\n- **Failed run:** https://github.com/{REPO}/actions/runs/{run_id}\n"}


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
	assert "L1: rejected" in artifact.read_text()
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


def test_collected_evidence_never_contains_artifact_assignment_secret(tmp_path: Path) -> None:
	fake = FakeGh()
	secret = "unique_assignment_value_6335"
	inline_secret = "unlabelled-credential-value-6335"
	fake.routes[f"repos/{REPO}/actions/artifacts/501/zip"] = _zip({
		"editor_attempt_1.err": f"::error::failed CUSTOM_SERVICE_PASSWORD={secret}\n::error::request failed with credential {inline_secret}\nReview isolation snapshot or transfer rejected\n".encode(),
	})
	_collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	out = tmp_path / "evidence"
	assert all(secret not in path.read_text() and inline_secret not in path.read_text() for path in out.rglob("*") if path.is_file())
	assert secret not in ev.render_prompt_section(str(out)) and inline_secret not in ev.render_prompt_section(str(out))
	run_meta = json.loads((out / f"runs/{REPO.replace('/', '__')}__111/meta.json").read_text())
	assert run_meta["artifact_format"] == ev.ARTIFACT_EXTRACT_FORMAT


def test_stale_cached_artifacts_are_recollected(tmp_path: Path) -> None:
	_collector(tmp_path, FakeGh()).collect(_issue(), [], issue_repo=REPO)
	run_dir = tmp_path / "evidence" / f"runs/{REPO.replace('/', '__')}__111"
	artifact_dir = run_dir / "artifact-codex-review-autofix-failure-logs-111-1"
	secret = "old_artifact_password_6335"
	(artifact_dir / "editor_attempt_1.err").write_text(f"CUSTOM_SERVICE_PASSWORD={secret}")
	(artifact_dir / "old.err").write_text(f"::error::{secret}")
	meta_path = run_dir / "meta.json"
	meta = json.loads(meta_path.read_text())
	meta["artifact_format"] = "allowlist-lines.v1"
	meta_path.write_text(json.dumps(meta))
	fake = FakeGh()
	_collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert f"repos/{REPO}/actions/artifacts/501/zip" in fake.paths
	assert not (artifact_dir / "old.err").exists()
	assert all(secret not in path.read_text() for path in (tmp_path / "evidence").rglob("*") if path.is_file())


def test_low_rate_removes_stale_artifacts(tmp_path: Path) -> None:
	_collector(tmp_path, FakeGh()).collect(_issue(), [], issue_repo=REPO)
	run_dir = tmp_path / "evidence" / f"runs/{REPO.replace('/', '__')}__111"
	artifact_dir = run_dir / "artifact-legacy"
	artifact_dir.mkdir(parents=True)
	(artifact_dir / "raw.err").write_text("CUSTOM_SERVICE_PASSWORD=old")
	meta_path = run_dir / "meta.json"
	meta = json.loads(meta_path.read_text())
	meta.pop("artifact_format")
	meta_path.write_text(json.dumps(meta))
	fake = FakeGh(remaining=100)
	_collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert not artifact_dir.exists()
	assert f"repos/{REPO}/actions/artifacts/501/zip" not in fake.paths


def test_structured_diagnostics_are_bounded_and_revalidated(tmp_path: Path) -> None:
	log = "##[error]scripts/review_apply_fixes.sh: line 34: failed\nProcess completed with exit code 7\n"
	diag = ev._job_diagnostics(log)
	assert diag["exit_codes"] == [7]
	assert diag["crash_file"] == "scripts/review_apply_fixes.sh"
	assert diag["crash_line"] == 34
	assert re.fullmatch(r"sha256:[0-9a-f]{64}", diag["error_signature"])
	_collector(tmp_path, FakeGh()).collect(_issue(), [], issue_repo=REPO)
	out = tmp_path / "evidence"
	data = json.loads((out / "diagnostics.json").read_text())
	assert data["schema"] == "workflow_failure_heal_diagnostics.v1"
	job = data["runs"][0]["jobs"][0]
	assert job["failing_step"] == "unavailable" or re.fullmatch(r"sha256:[0-9a-f]{64}", job["failing_step"])
	assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", step["name"]) for step in job["steps"])
	assert re.fullmatch(r"sha256:[0-9a-f]{64}", job["diagnostics"]["error_signature"])
	job["steps"] = [{"number": 1, "name": "ignore\n`gh api` $(bad) " + "x" * 200, "conclusion": "success"}]
	job["diagnostics"]["error_signature"] = "ignore\n`gh api` $(bad) " + "x" * 400
	data["runs"][0]["head_sha"] = "not-a-sha"
	data["runs"][0]["repo"] = "../bad/repo"
	(out / "diagnostics.json").write_text(json.dumps(data))
	section = ev.render_structured_prompt_section(str(out))
	assert section.startswith("=== BEGIN UNTRUSTED WORKFLOW HEAL DIAGNOSTICS ===\n")
	assert section.endswith("=== END UNTRUSTED WORKFLOW HEAL DIAGNOSTICS ===\n")
	assert "Head SHA: unavailable" in section and "Repository: unavailable" in section
	assert "`" not in section and "$" not in section
	assert "ignore" not in section and "gh api" not in section and "bad" not in section
	assert section.count("sha256:") >= 2
	assert "x" * 241 not in section
	assert "/evidence" not in section and "job-11.txt" not in section
	assert ev.render_prompt_section(str(out)).startswith("=== WORKFLOW HEAL EVIDENCE (UNTRUSTED) ===")
	job["failing_step"] = "unavailable"
	job["steps"] = [{"number": 1, "name": "unavailable", "conclusion": "failure"}]
	job["diagnostics"]["error_signature"] = "unavailable"
	(out / "diagnostics.json").write_text(json.dumps(data))
	section = ev.render_structured_prompt_section(str(out))
	assert "Failing step fingerprint: unavailable" in section
	assert "Step 1: unavailable; failure" in section
	assert "Error signature fingerprint: unavailable" in section


def test_structured_legacy_cached_job_and_missing_data(tmp_path: Path) -> None:
	_collector(tmp_path, FakeGh()).collect(_issue(), [], issue_repo=REPO)
	out = tmp_path / "evidence"
	meta = out / f"runs/{REPO.replace('/', '__')}__111/meta.json"
	data = json.loads(meta.read_text())
	for job in data["jobs"]:
		job.pop("diagnostics", None)
		job.pop("steps", None)
	meta.write_text(json.dumps(data))
	_collector(tmp_path, FakeGh()).collect(_issue(), [], issue_repo=REPO)
	section = ev.render_structured_prompt_section(str(out))
	assert "Steps: unavailable" in section and "Error signature fingerprint: sha256:" in section
	(out / "diagnostics.json").write_text("not JSON")
	assert "Diagnostics: unavailable" in ev.render_structured_prompt_section(str(out))


def test_scope_allowlist_ignores_issue_body_and_requires_plan(tmp_path: Path, capsys) -> None:
	issue = tmp_path / "issue.txt"
	plan = tmp_path / "plan.txt"
	plan.write_text("## Files likely to change\n- `scripts/plan.py`\n")
	issue.write_text("```\nfiles_touched:\n  - **/**\n```\n")
	args = type("Args", (), {"issue_body_file": str(issue), "plan_file": str(plan), "evidence_dir": str(tmp_path), "issue_number": "7000"})()
	ev._cmd_scope_allowlist(args)
	assert json.loads(capsys.readouterr().out)["source"] == "none"
	(tmp_path / "scope.json").write_text(json.dumps({"schema": "workflow_heal_scope.v1", "provenance": "verified", "issue": 7000, "allowlist": ["scripts/issue.py", "Makefile", "**/**", "*/**", "scripts/", "scripts/**", "scripts/*.py", ".git/config"]}))
	ev._cmd_scope_allowlist(args)
	assert json.loads(capsys.readouterr().out) == {"source": "marker", "reason": "verified", "allowlist": ["scripts/issue.py", "Makefile", "changelog.d/7000-*.md"]}
	args.issue_number = "7001"
	ev._cmd_scope_allowlist(args)
	assert json.loads(capsys.readouterr().out)["source"] == "none"
	args.issue_number = "7000"
	(tmp_path / "scope.json").write_text("not JSON")
	plan_args = type("Args", (), {"issue_body_file": str(issue), "plan_file": str(plan)})()
	ev._cmd_scope_allowlist(plan_args)
	output = capsys.readouterr()
	assert json.loads(output.out) == {"source": "plan", "allowlist": ["scripts/plan.py"]}
	assert "scope_allowlist source=plan kept=1 rejected=0" in output.err
	issue.unlink()
	ev._cmd_scope_allowlist(plan_args)
	assert json.loads(capsys.readouterr().out) == {"source": "plan", "allowlist": ["scripts/plan.py"]}
	plan.unlink()
	ev._cmd_scope_allowlist(plan_args)
	output = capsys.readouterr()
	assert json.loads(output.out) == {"source": "none", "allowlist": []}
	assert "scope_allowlist source=none kept=0 rejected=0 reason=exception" in output.err
	plan.write_text("no plan paths")
	ev._cmd_scope_allowlist(args)
	assert json.loads(capsys.readouterr().out)["source"] == "none"
	(tmp_path / "scope.json").write_text(json.dumps({"schema": "other", "provenance": "verified", "issue": 7000, "allowlist": ["scripts/issue.py"]}))
	ev._cmd_scope_allowlist(args)
	assert json.loads(capsys.readouterr().out)["source"] == "none"


def test_scope_collect_uses_only_verified_leading_issue_marker(tmp_path: Path) -> None:
	fake = FakeGh()
	issue = _issue()
	marker = "<!-- workflow-failure-heal:scope=scripts/fix.py,tests/test_fix.py -->\n"
	fake.routes["provenance"]["data"]["issue"]["issue"]["body"] = issue["body"].replace("- **Target branch:**", marker + "- **Target branch:**")
	fake.routes["provenance"]["data"]["issue"]["issue"].update({"lastEditedAt": "2026-10-04T12:00:00Z", "editor": {"login": "healer"}, "userContentEdits": {"totalCount": 1, "nodes": [{"editor": {"login": "healer"}}]}})
	_collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	scope_path = tmp_path / "evidence" / "scope.json"
	assert json.loads(scope_path.read_text())["allowlist"] == ["scripts/fix.py", "tests/test_fix.py"]
	assert json.loads(scope_path.read_text())["issue"] == 7000
	# Reused evidence folders cannot retain a prior verified scope on an unverified read.
	fake.routes["provenance"]["data"]["issue"]["issue"]["editor"] = {"login": "intruder"}
	fake.routes["provenance"]["data"]["issue"]["issue"]["lastEditedAt"] = "2026-10-04T12:00:00Z"
	_collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert not scope_path.exists()
	# A comment or later body prose cannot introduce a leading marker.
	fake.routes["provenance"]["data"]["issue"]["issue"].update({"editor": None, "lastEditedAt": None, "userContentEdits": {"totalCount": 0, "nodes": []}, "body": issue["body"] + "\n" + marker})
	_collector(tmp_path, fake).collect(issue, [_occurrence(222) | {"body": marker + _occurrence(222)["body"]}], issue_repo=REPO)
	assert not scope_path.exists()


def test_scope_allowlist_only_concrete_unprotected_plan_files(tmp_path: Path, capsys) -> None:
	plan = tmp_path / "plan.txt"
	paths = [
		"scripts/", "scripts", ".github/workflows/", "scripts/*.py",
		"scripts/files_touched_scope_guard.py", "Scripts/Workflow_Failure_Heal_Evidence.py",
		"./scripts/implement_commit_changes.sh", "scripts/implement_commit_changes.sh", ".github/ai/claude_engine.json",
		".CLAUDE/hooks/gh_api_write_guard.py", "docs/.GIT/config.txt",
		"scripts/fix.py", ".github/workflows/ci.yml", "prompts/fix.txt",
		"tests/test_x.py", "scripts/fix.py",
	]
	plan.write_text("## Files likely to change\n" + "".join(f"- `{path}`\n" for path in paths))
	args = type("Args", (), {"issue_body_file": str(tmp_path / "missing"), "plan_file": str(plan)})()
	ev._cmd_scope_allowlist(args)
	output = capsys.readouterr()
	assert json.loads(output.out) == {
		"source": "plan",
		"allowlist": ["scripts/fix.py", ".github/workflows/ci.yml", "prompts/fix.txt", "tests/test_x.py"],
	}
	assert "scope_allowlist source=plan kept=4 rejected=" in output.err
	assert "scripts/files_touched_scope_guard.py" not in output.err
	assert ".github/ai/claude_engine.json" not in output.err
	plan.write_text("## Files likely to change\n- `scripts/files_touched_scope_guard.py`\n")
	ev._cmd_scope_allowlist(args)
	assert json.loads(capsys.readouterr().out) == {"source": "none", "allowlist": []}


def test_scope_allowlist_rejects_all_unsafe_entries(tmp_path: Path, capsys) -> None:
	plan = tmp_path / "plan.txt"
	plan.write_text("## Files likely to change\n" + "".join(
		f"- `{path}`\n" for path in ("scripts/*.py", "docs/.GIT/config.txt", "../escape.py", "scripts/./a.py", "scripts/a\u2028b.py", "scripts/")
	))
	args = type("Args", (), {"issue_body_file": str(tmp_path / "missing"), "plan_file": str(plan)})()
	ev._cmd_scope_allowlist(args)
	assert json.loads(capsys.readouterr().out) == {"source": "none", "allowlist": []}
	for path in (" scripts/fix.py", "scripts/fix.py ", "scripts/a\u2028b.py"):
		assert ev._heal_scope_entry_reject_reason(path) == "whitespace"
	for path in ("scripts/./a.py", "../escape.py", "docs/.GIT/config.txt", "scripts//a.py", "/tmp/a.py", "scripts\\a.py"):
		assert ev._heal_scope_entry_reject_reason(path) == "invalid_path"
	assert ev._heal_scope_entry_reject_reason("scripts/") == "directory"
	assert ev._heal_scope_entry_reject_reason("scripts/*.py") == "glob"
	assert ev._heal_scope_entry_reject_reason("Makefile") == "no_extension"


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
	assert manifest["api_calls"] <= 9  # Both run identities are in the head-SHA timeline.
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
	assert {"part": "run:acme/registered:111", "reason": "unverified_intake_provenance"} in manifest["skipped"]
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


def test_invalid_issue_identity_logs_provenance_reason(tmp_path: Path, capsys) -> None:
	result = _collector(tmp_path, FakeGh())._verify_provenance(_issue(number=0), [], REPO)
	assert result["reason"] == "issue_identity_invalid"
	assert "provenance outcome=unverified reason=issue_identity_invalid comments_verified=0/0" in capsys.readouterr().err


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
	assert {"part": f"run:{REPO}:222", "reason": "unverified_intake_provenance"} in manifest["skipped"]
	assert f"run:{REPO}:222: unverified_intake_provenance" in (tmp_path / "evidence/INDEX.md").read_text()


def test_unverified_issue_body_run_is_recorded_without_fetch(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes["provenance"]["data"]["issue"]["issue"]["userContentEdits"] = {"totalCount": 1, "nodes": [{"editor": {"login": "other"}}]}
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_intake_provenance"} in manifest["skipped"]
	assert not any("/actions/jobs/" in path for path in fake.paths)


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
		{"id": 111, "name": "Internal: AI Review & Autofix", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "repository": {"full_name": "acme/registered"}, "status": "completed", "conclusion": "success"},
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
	issue = _issue(body=_issue()["body"].replace(REPO, "acme/registered").replace("Internal: AI Review & Autofix", "AI Review"))
	fake = FakeGh()
	fake.routes["repos/acme/registered/actions/runs/111"] = {
		"id": 111, "head_sha": HEAD_SHA, "head_branch": "main", "name": "AI Review [pr:6133]", "repository": {"full_name": "acme/registered"}, "status": "completed", "conclusion": "success",
	}
	fake.routes["repos/acme/registered/actions/runs/111/jobs?per_page=100"] = fake.routes[f"repos/{REPO}/actions/runs/111/jobs?per_page=100"]
	fake.routes["repos/acme/registered/actions/jobs/11/logs"] = _review_log()
	fake.routes["repos/acme/registered/actions/runs/111/artifacts?per_page=100"] = {"artifacts": []}
	_collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert "repos/acme/registered/actions/runs/111" in fake.paths
	assert "repos/acme/registered/actions/runs/111/jobs?per_page=100" in fake.paths


def test_same_repo_default_branch_review_run_is_accepted_with_pr_token(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"] = {"workflow_runs": []}
	fake.routes[f"repos/{REPO}/actions/runs/111"] = {
		"id": 111, "head_sha": HEAD_SHA, "head_branch": "main",
		"name": "Internal: AI Review & Autofix [pr:6133]", "repository": {"full_name": REPO},
		"status": "completed", "conclusion": "success",
	}
	_collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert f"repos/{REPO}/actions/runs/111" in fake.paths
	assert f"repos/{REPO}/actions/runs/111/jobs?per_page=100" in fake.paths


def test_pr_named_run_on_other_head_is_skipped_before_jobs(tmp_path: Path, monkeypatch) -> None:
	monkeypatch.setenv("WORKFLOW_HEAL_CONSUMER_REGISTRY", str(tmp_path / "consumers.json"))
	(tmp_path / "consumers.json").write_text(json.dumps(["acme/registered"]))
	issue = _issue(body=_issue()["body"].replace(REPO, "acme/registered"))
	fake = FakeGh()
	fake.routes["repos/acme/registered/actions/runs/111"] = {
		"id": 111, "head_sha": MERGE_SHA, "head_branch": "main", "name": "AI Review [pr:6133]", "repository": {"full_name": "acme/registered"},
	}
	manifest = _collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert {"part": "run:acme/registered:111", "reason": "unverified_run_head_mismatch"} in manifest["skipped"]
	assert "run:acme/registered:111: unverified_run_head_mismatch" in (tmp_path / "evidence/INDEX.md").read_text()
	assert "repos/acme/registered/actions/runs/111/jobs?per_page=100" not in fake.paths


def test_pr_linked_run_requires_reported_head(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes["repos/acme/registered/actions/runs/111"] = {
		"id": 111, "head_sha": MERGE_SHA, "head_branch": "ai/issue-5144",
		"name": "Internal: AI Review & Autofix", "pull_requests": [{"number": 6133}], "repository": {"full_name": "acme/registered"}, "status": "completed", "conclusion": "failure",
	}
	ref = {"repo": "acme/registered", "run_id": "111", "source_repo": "acme/registered", "source_number": "6133", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "workflow_name": "Internal: AI Review & Autofix"}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_head_mismatch")
	fake.routes["repos/acme/registered/actions/runs/111"]["head_sha"] = HEAD_SHA
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (True, "verified")


@pytest.mark.parametrize("cached", [False, True])
def test_same_repo_unrelated_run_is_skipped_before_jobs(tmp_path: Path, cached: bool) -> None:
	fake = FakeGh()
	fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"] = {"workflow_runs": []}
	fake.routes[f"repos/{REPO}/actions/runs/222"] = {
		"id": 222, "head_sha": MERGE_SHA, "head_branch": "other", "pull_requests": [], "repository": {"full_name": REPO},
	}
	stale = tmp_path / "evidence/runs/acme__coding-workflows__222"
	if cached:
		stale.mkdir(parents=True)
		(stale / "meta.json").write_text('{"complete": true}')
	manifest = _collector(tmp_path, fake).collect(_issue(), [_occurrence(222)], issue_repo=REPO)
	assert {"part": f"run:{REPO}:222", "reason": "unverified_run_head_mismatch"} in manifest["skipped"]
	assert f"run:{REPO}:222: unverified_run_head_mismatch" in (tmp_path / "evidence/INDEX.md").read_text()
	assert f"repos/{REPO}/actions/runs/222/jobs?per_page=100" not in fake.paths
	assert not stale.exists()


@pytest.mark.parametrize("reported_sha", ["", "not-a-sha"])
def test_same_repo_missing_head_skips_metadata_fetch(tmp_path: Path, reported_sha: str) -> None:
	fake = FakeGh()
	ref = {"repo": REPO, "run_id": "222", "source_repo": REPO, "source_number": "6133", "head_sha": reported_sha, "head_branch": "ai/issue-5144"}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_no_verified_context")
	assert fake.paths == []


def test_same_repo_run_from_other_source_is_rejected_without_fetch(tmp_path: Path) -> None:
	fake = FakeGh()
	ref = {"repo": REPO, "run_id": "222", "source_repo": "acme/registered", "source_number": "6133", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144"}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_no_verified_context")
	assert fake.paths == []


def test_same_repo_run_metadata_rejects_other_repository(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes[f"repos/{REPO}/actions/runs/222"]["repository"] = {"full_name": "other/repo"}
	ref = {"repo": REPO, "run_id": "222", "source_repo": REPO, "source_number": "6133", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "workflow_name": "Internal: AI Review & Autofix"}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_repo_mismatch")


def test_same_repo_timeline_hit_needs_no_run_get(tmp_path: Path) -> None:
	fake = FakeGh()
	collector = _collector(tmp_path, fake)
	collector.timeline(ev.heal_context(_issue()))
	ref = {"repo": REPO, "run_id": "111", "source_repo": REPO, "source_number": "6133", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "workflow_name": "Internal: AI Review & Autofix"}
	assert collector._verify_run(ref, REPO) == (True, "verified_review_only")
	assert f"repos/{REPO}/actions/runs/111" not in fake.paths


@pytest.mark.parametrize("timeline_hit", [True, False])
@pytest.mark.parametrize("cached", [False, True])
def test_matching_branch_cannot_override_different_linked_pr(tmp_path: Path, timeline_hit: bool, cached: bool) -> None:
	issue = _issue()
	issue["body"] += f"- **Source pull request:** https://github.com/{REPO}/pull/6133 ({REPO}#6133)\n"
	fake = FakeGh()
	timeline_path = f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"
	run = fake.routes[timeline_path]["workflow_runs"][0]
	run["name"] = "Internal: AI Review & Autofix"
	run["pull_requests"] = [{"number": 7777}]
	if not timeline_hit:
		fake.routes[timeline_path] = {"workflow_runs": []}
		fake.routes[f"repos/{REPO}/actions/runs/111"] = run
	stale = tmp_path / "evidence/runs/acme__coding-workflows__111"
	if cached:
		stale.mkdir(parents=True)
		(stale / "meta.json").write_text('{"complete": true}')
	manifest = _collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_pr_mismatch"} in manifest["skipped"]
	assert f"run:{REPO}:111: unverified_run_pr_mismatch" in (tmp_path / "evidence/INDEX.md").read_text()
	assert not any("/actions/runs/111/jobs" in path or "/actions/jobs/" in path or "/actions/runs/111/artifacts" in path for path in fake.paths)
	assert not stale.exists()


@pytest.mark.parametrize("from_comment", [False, True])
def test_issue_origin_can_use_matching_branch_with_different_linked_pr(tmp_path: Path, from_comment: bool) -> None:
	issue = _issue()
	comments = []
	if from_comment:
		comment = _occurrence(111)
		comment["body"] = comment["body"].replace("Source pull request", "Source issue").replace("/pull/6133", "/issues/6133")
		comments.append(comment)
	else:
		issue["body"] += f"- **Source issue:** https://github.com/{REPO}/issues/6133 ({REPO}#6133)\n"
	fake = FakeGh()
	run = fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"]["workflow_runs"][0]
	run["name"] = "Internal: AI Review & Autofix [pr:7777]"
	run["pull_requests"] = [{"number": 7777}]
	manifest = _collector(tmp_path, fake).collect(issue, comments, issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_pr_mismatch"} not in manifest["skipped"]
	assert f"repos/{REPO}/actions/runs/111/jobs?per_page=100" in fake.paths


def test_issue_origin_cannot_use_matching_pr_token_without_branch(tmp_path: Path) -> None:
	fake = FakeGh()
	ref = {"repo": REPO, "run_id": "111", "source_repo": REPO, "source_number": "6133", "source_kind": "issue", "head_sha": HEAD_SHA, "head_branch": "other", "workflow_name": "Internal: AI Review & Autofix"}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_head_mismatch")
	ref["head_branch"] = ""
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_no_verified_context")


def test_same_repo_run_without_head_context_is_skipped_before_fetch(tmp_path: Path) -> None:
	fake = FakeGh()
	ref = {"repo": REPO, "run_id": "111", "source_repo": REPO, "source_number": "6133", "head_sha": "", "head_branch": "ai/issue-5144"}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_no_verified_context")
	assert fake.paths == []


def test_same_repo_unrelated_run_is_rejected_before_jobs(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"] = {"workflow_runs": []}
	fake.routes[f"repos/{REPO}/actions/runs/111"] = {
		"id": 111, "head_sha": MERGE_SHA, "head_branch": "ai/issue-5144",
		"pull_requests": [{"number": 6133}], "repository": {"full_name": REPO},
	}
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_head_mismatch"} in manifest["skipped"]
	assert f"run:{REPO}:111: unverified_run_head_mismatch" in (tmp_path / "evidence/INDEX.md").read_text()
	assert not any("/actions/jobs/" in path or "/actions/runs/111/jobs" in path or "/actions/runs/111/artifacts" in path for path in fake.paths)


def test_same_repo_matching_head_and_branch_uses_timeline_or_get(tmp_path: Path) -> None:
	fake = FakeGh()
	ref = {"repo": REPO, "run_id": "111", "source_repo": REPO, "source_number": "", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "workflow_name": "Internal: AI Review & Autofix"}
	collector = _collector(tmp_path, fake)
	fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"]["workflow_runs"].pop()
	fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"]["workflow_runs"][0]["name"] = "Internal: AI Review & Autofix"
	collector.timeline({"source_repo": REPO, "head_sha": HEAD_SHA})
	assert collector._verify_run(ref, REPO) == (True, "verified_review_only")
	assert f"repos/{REPO}/actions/runs/111" not in fake.paths
	ref["run_id"] = "222"
	assert collector._verify_run(ref, REPO) == (True, "verified_review_only")
	assert f"repos/{REPO}/actions/runs/222" in fake.paths


@pytest.mark.parametrize("timeline_hit", [True, False])
@pytest.mark.parametrize("run_workflow", ["Unrelated CI [pr:6133]", None])
def test_wrong_workflow_skips_same_head_run_and_pruned_cache(tmp_path: Path, timeline_hit: bool, run_workflow: str | None) -> None:
	fake = FakeGh()
	wrong_run = fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"]["workflow_runs"][0]
	wrong_run["name"] = run_workflow
	if not timeline_hit:
		fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"] = {"workflow_runs": []}
		fake.routes[f"repos/{REPO}/actions/runs/111"] = wrong_run
	stale = tmp_path / "evidence/runs/acme__coding-workflows__111"
	stale.mkdir(parents=True)
	(stale / "meta.json").write_text('{"complete": true}')
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_workflow_mismatch"} in manifest["skipped"]
	assert "unverified_run_workflow_mismatch" in (tmp_path / "evidence/INDEX.md").read_text()
	assert not stale.exists()
	assert not any("/actions/jobs/" in path or "/actions/runs/111/jobs" in path or "/actions/runs/111/artifacts" in path for path in fake.paths)
	assert (f"repos/{REPO}/actions/runs/111" in fake.paths) is not timeline_hit


def test_reported_workflow_is_required_before_run_lookup(tmp_path: Path) -> None:
	issue = _issue(body=_issue()["body"].replace("- **Failed workflow:** `Internal: AI Review & Autofix` (conclusion: `success`)\n", ""))
	fake = FakeGh()
	manifest = _collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_no_verified_context"} in manifest["skipped"]
	assert f"repos/{REPO}/actions/runs/111" not in fake.paths
	assert f"repos/{REPO}/actions/runs/111/jobs?per_page=100" not in fake.paths


def test_occurrence_run_uses_its_own_reported_workflow(tmp_path: Path) -> None:
	fake = FakeGh()
	comment = _occurrence(222)
	comment["body"] = comment["body"].replace("Internal: AI Review & Autofix", "Other Workflow")
	fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"]["workflow_runs"][1]["name"] = "Other Workflow"
	refs = ev.trusted_run_refs(_issue(), [comment], allowed_repos=[REPO], limit=3, verified_comment_ids={222})
	assert [ref["workflow_name"] for ref in refs] == ["Internal: AI Review & Autofix", "Other Workflow"]
	manifest = _collector(tmp_path, fake).collect(_issue(), [comment], issue_repo=REPO)
	assert {"part": f"run:{REPO}:222", "reason": "unverified_run_workflow_mismatch"} not in manifest["skipped"]
	assert f"repos/{REPO}/actions/runs/222/jobs?per_page=100" in fake.paths


def test_same_repo_run_rejected_when_source_is_another_repo(tmp_path: Path, monkeypatch) -> None:
	fake = FakeGh()
	ref = {"repo": REPO, "run_id": "111", "source_repo": "acme/registered", "source_number": "6133", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144"}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_no_verified_context")
	assert fake.paths == []
	registry = tmp_path / "consumers.json"
	registry.write_text(json.dumps(["acme/registered"]))
	monkeypatch.setenv("WORKFLOW_HEAL_CONSUMER_REGISTRY", str(registry))
	issue = _issue(body=_issue()["body"].replace(f"source={REPO}#6133", "source=acme/registered#6133"))
	manifest = _collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_no_verified_context"} in manifest["skipped"]
	assert f"repos/{REPO}/actions/runs/111/jobs?per_page=100" not in fake.paths


def test_escalation_issue_without_head_skips_same_repo_runs(tmp_path: Path) -> None:
	issue = _issue(body=_issue()["body"].replace(f"- **Head SHA:** `{HEAD_SHA}`\n", ""))
	fake = FakeGh()
	manifest = _collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_no_verified_context"} in manifest["skipped"]
	assert f"run:{REPO}:111: unverified_run_no_verified_context" in (tmp_path / "evidence/INDEX.md").read_text()
	assert not any("/actions/jobs/" in path or "/actions/runs/111/jobs" in path or "/actions/runs/111/artifacts" in path or path == f"repos/{REPO}/actions/runs/111" for path in fake.paths)
	assert (tmp_path / "evidence/provenance.json").is_file()
	assert (tmp_path / "evidence/lineage.json").is_file()
	assert (tmp_path / "evidence/timeline.json").is_file()


def test_same_repo_run_metadata_and_conclusion_are_required(tmp_path: Path) -> None:
	fake = FakeGh()
	ref = {"repo": REPO, "run_id": "111", "source_repo": REPO, "source_number": "6133", "head_sha": "", "head_branch": "ai/issue-5144", "workflow_name": "Internal: AI Review & Autofix"}
	collector = _collector(tmp_path, fake)
	assert collector._verify_run(ref, REPO) == (False, "run_no_verified_context")
	assert fake.paths == []
	ref["head_sha"] = HEAD_SHA
	run = fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"]["workflow_runs"][0].copy()
	fake.routes[f"repos/{REPO}/actions/runs/111"] = run
	run["head_sha"] = MERGE_SHA
	assert collector._verify_run(ref, REPO) == (False, "run_head_mismatch")
	run["head_sha"] = HEAD_SHA
	run["repository"] = {"full_name": "other/repo"}
	assert collector._verify_run(ref, REPO) == (False, "run_repo_mismatch")
	run["repository"] = {"full_name": REPO}
	run["conclusion"] = "neutral"
	assert collector._verify_run(ref, REPO) == (False, "run_not_failed")
	run["conclusion"] = "failure"
	assert collector._verify_run(ref, REPO) == (True, "verified")
	run["conclusion"] = "success"
	assert collector._verify_run(ref, REPO) == (True, "verified_review_only")
	run["conclusion"] = None
	assert collector._verify_run(ref, REPO) == (False, "run_not_failed")
	run["status"] = "in_progress"
	assert collector._verify_run(ref, REPO) == (True, "verified_review_only")


def test_release_run_with_matching_head_and_branch_is_collected(tmp_path: Path) -> None:
	issue = _issue(body=_issue()["body"].replace(f"source={REPO}#6133", f"source={REPO}#run").replace("ai/issue-5144", "stable").replace("Internal: AI Review & Autofix", "Promote main to stable"))
	fake = FakeGh()
	fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"] = {"workflow_runs": [
		{"id": 111, "name": "Promote main to stable", "head_sha": HEAD_SHA, "head_branch": "stable", "repository": {"full_name": REPO}, "status": "completed", "conclusion": "failure"},
	]}
	_collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert f"repos/{REPO}/actions/runs/111/jobs?per_page=100" in fake.paths
	assert f"repos/{REPO}/actions/runs/111" not in fake.paths


def test_successful_non_review_run_skips_logs_artifacts_and_cached_evidence(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes[f"repos/{REPO}/actions/runs/111/jobs?per_page=100"] = {"jobs": [
		{"id": 10, "name": "build", "conclusion": "success", "status": "completed"},
	]}
	run_dir = tmp_path / "evidence" / f"runs/{REPO.replace('/', '__')}__111"
	run_dir.mkdir(parents=True)
	(run_dir / "meta.json").write_text(json.dumps({"complete": True, "job_table": [{"name": "build", "conclusion": "success"}]}))
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_not_failed"} in manifest["skipped"]
	assert "unverified_run_not_failed" in (tmp_path / "evidence/INDEX.md").read_text()
	assert not run_dir.exists()
	assert not any("/actions/jobs/" in path or "/artifacts" in path for path in fake.paths)
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_not_failed"} in manifest["skipped"]
	assert not any("/actions/jobs/" in path or "/artifacts" in path for path in fake.paths)


def test_review_only_run_does_not_trust_malformed_cached_job_table(tmp_path: Path) -> None:
	fake = FakeGh()
	run_dir = tmp_path / "evidence" / f"runs/{REPO.replace('/', '__')}__111"
	run_dir.mkdir(parents=True)
	(run_dir / "meta.json").write_text(json.dumps({"complete": True, "job_table": ["review / codex-agent"]}))
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_not_failed"} in manifest["skipped"]
	assert not run_dir.exists()
	assert not any("/actions/jobs/" in path or "/artifacts" in path for path in fake.paths)


def test_review_only_run_rejects_empty_cached_review_jobs(tmp_path: Path) -> None:
	fake = FakeGh()
	run_dir = tmp_path / "evidence" / f"runs/{REPO.replace('/', '__')}__111"
	run_dir.mkdir(parents=True)
	(run_dir / "meta.json").write_text(json.dumps({"complete": True, "job_table": [{"name": "review / codex-agent"}], "jobs": []}))
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_not_failed"} in manifest["skipped"]
	assert not run_dir.exists()


@pytest.mark.parametrize("run_status,run_conclusion", [("completed", "success"), ("in_progress", None)])
def test_review_only_run_ignores_failed_non_review_jobs(tmp_path: Path, run_status: str, run_conclusion: str | None) -> None:
	fake = FakeGh()
	fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"]["workflow_runs"][0].update({"status": run_status, "conclusion": run_conclusion})
	fake.routes[f"repos/{REPO}/actions/runs/111/jobs?per_page=100"]["jobs"].insert(0, {
		"id": 10, "name": "build", "conclusion": "failure", "status": "completed",
	})
	fake.routes[f"repos/{REPO}/actions/jobs/10/logs"] = b"non-review job log"
	run_dir = tmp_path / "evidence" / f"runs/{REPO.replace('/', '__')}__111"
	run_dir.mkdir(parents=True)
	(run_dir / "job-10.txt").write_text("stale non-review job log")
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert f"repos/{REPO}/actions/jobs/11/logs" in fake.paths
	assert f"repos/{REPO}/actions/jobs/10/logs" not in fake.paths
	assert not (run_dir / "job-10.txt").exists()
	assert (run_dir / "job-11.txt").is_file()
	assert all(item["path"] != f"runs/{REPO.replace('/', '__')}__111/job-10.txt" for item in manifest["files"])


def test_review_only_run_with_only_failed_non_review_job_skips_evidence(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes[f"repos/{REPO}/actions/runs/111/jobs?per_page=100"] = {"jobs": [
		{"id": 10, "name": "build", "conclusion": "failure", "status": "completed"},
	]}
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_not_failed"} in manifest["skipped"]
	assert not any("/actions/jobs/" in path or "/artifacts" in path for path in fake.paths)


def test_review_only_run_rejects_cached_non_review_log_even_with_review_job(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes[f"repos/{REPO}/actions/runs/111"]["conclusion"] = "failure"
	fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"]["workflow_runs"][0]["conclusion"] = "failure"
	fake.routes[f"repos/{REPO}/actions/runs/111/jobs?per_page=100"]["jobs"].insert(0, {
		"id": 10, "name": "build", "conclusion": "failure", "status": "completed",
	})
	fake.routes[f"repos/{REPO}/actions/jobs/10/logs"] = b"non-review job log"
	_collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	run_dir = tmp_path / "evidence" / f"runs/{REPO.replace('/', '__')}__111"
	assert (run_dir / "job-10.txt").exists()
	fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"]["workflow_runs"][0]["conclusion"] = "success"
	fake.paths.clear()
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_not_failed"} in manifest["skipped"]
	assert not run_dir.exists()
	assert not any("/actions/jobs/" in path or "/artifacts" in path for path in fake.paths)


def test_review_only_run_rejects_orphaned_cached_non_review_log(tmp_path: Path) -> None:
	_collector(tmp_path, FakeGh()).collect(_issue(), [], issue_repo=REPO)
	run_dir = tmp_path / "evidence" / f"runs/{REPO.replace('/', '__')}__111"
	(run_dir / "job-10.txt").write_text("old non-review log")
	fake = FakeGh()
	manifest = _collector(tmp_path, fake).collect(_issue(), [], issue_repo=REPO)
	assert {"part": f"run:{REPO}:111", "reason": "unverified_run_not_failed"} in manifest["skipped"]
	assert not run_dir.exists()
	assert not any("/actions/jobs/" in path or "/artifacts" in path for path in fake.paths)


def test_same_repo_release_run_with_branch_and_failed_conclusion(tmp_path: Path) -> None:
	issue = _issue(body=_issue()["body"].replace(f"{REPO}#6133", f"{REPO}#run").replace("Internal: AI Review & Autofix", "Promote main to stable"))
	fake = FakeGh()
	run = fake.routes[f"repos/{REPO}/actions/runs?head_sha={HEAD_SHA}&per_page=30"]["workflow_runs"][0]
	run.update({"name": "Promote main to stable", "conclusion": "failure", "pull_requests": []})
	_collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert f"repos/{REPO}/actions/jobs/11/logs" in fake.paths


@pytest.mark.parametrize("reported_sha", ["", "not-a-sha"])
def test_cross_repo_missing_head_skips_metadata_fetch(tmp_path: Path, reported_sha: str) -> None:
	fake = FakeGh()
	ref = {"repo": "acme/registered", "run_id": "111", "source_repo": "acme/registered", "source_number": "6133", "head_sha": reported_sha, "head_branch": "ai/issue-5144"}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_no_verified_context")
	assert fake.paths == []


def test_cross_repo_head_without_branch_or_pr_is_not_enough(tmp_path: Path) -> None:
	fake = FakeGh()
	ref = {"repo": "acme/registered", "run_id": "111", "source_repo": "acme/registered", "source_number": "", "head_sha": HEAD_SHA, "head_branch": ""}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_no_verified_context")
	assert fake.paths == []


def test_cross_repo_run_without_repository_metadata_is_rejected(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes["repos/acme/registered/actions/runs/111"] = {
		"id": 111, "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144",
	}
	ref = {"repo": "acme/registered", "run_id": "111", "source_repo": "acme/registered", "source_number": "6133", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "workflow_name": "Internal: AI Review & Autofix"}
	assert _collector(tmp_path, fake)._verify_run(ref, REPO) == (False, "run_repo_mismatch")


def test_run_metadata_rejects_different_repository(tmp_path: Path) -> None:
	fake = FakeGh()
	fake.routes["repos/acme/registered/actions/runs/111"] = {
		"id": 111, "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "repository": {"full_name": "other/repo"},
	}
	ref = {"repo": "acme/registered", "run_id": "111", "source_repo": "acme/registered", "source_number": "6133", "head_sha": HEAD_SHA, "head_branch": "ai/issue-5144", "workflow_name": "Internal: AI Review & Autofix"}
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


def test_unverified_same_repo_cached_run_is_removed(tmp_path: Path) -> None:
	stale = tmp_path / "evidence/runs/acme__coding-workflows__111"
	stale.mkdir(parents=True)
	(stale / "meta.json").write_text('{"complete": true}')
	issue = _issue(body=_issue()["body"].replace(f"- **Head SHA:** `{HEAD_SHA}`\n", ""))
	fake = FakeGh()
	_collector(tmp_path, fake).collect(issue, [], issue_repo=REPO)
	assert not stale.exists()
	assert not any("/actions/runs/111/jobs" in path or "/actions/runs/111/artifacts" in path for path in fake.paths)


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
	# Structured diagnostics survive even when the size cap also removes a raw log.
	assert not (out / f"{run_dir}__111/artifact-codex-review-autofix-failure-logs-111-1").exists() or not any((out / f"{run_dir}__111/artifact-codex-review-autofix-failure-logs-111-1").iterdir())
	assert not (out / f"{run_dir}__111/job-11.txt").exists()
	assert (out / "diagnostics.json").exists()
	assert "(dropped: size limit)" in (out / "INDEX.md").read_text()
	index = (out / "INDEX.md").read_text()
	# The index never points at a file the budget removed.
	for line in index.splitlines():
		for path in __import__("re").findall(r"`/evidence/([^`]+)`", line):
			assert (out / path).exists() or path.endswith("/"), path
	second = FakeGh()
	second_manifest = _collector(tmp_path, second, max_total_bytes=12_000, max_file_bytes=6000).collect(_issue(), [_occurrence(222)], issue_repo=REPO)
	assert {"part": f"{run_dir}__111/job-11.txt", "reason": "cached_file_missing"} in second_manifest["skipped"]
	assert {"part": f"{run_dir}__111/artifact-codex-review-autofix-failure-logs-111-1/editor_attempt_1.err", "reason": "cached_file_missing"} in second_manifest["skipped"]
	second_index = (out / "INDEX.md").read_text()
	assert f"{run_dir}__111/job-11.txt: cached_file_missing" in second_index
	assert any(
		'job "review / codex-agent" (success), failing step: none (focus job)' in rendered_cache_job_entry
		and rendered_cache_job_entry.endswith("(missing from cache)")
		for rendered_cache_job_entry in second_index.splitlines()
	)
	assert not any("/actions/jobs/" in path for path in second.paths)


def test_current_budget_drop_is_not_a_cache_miss(tmp_path: Path) -> None:
	_collector(tmp_path, FakeGh()).collect(_issue(), [], issue_repo=REPO)
	second = FakeGh()
	manifest = _collector(tmp_path, second, max_total_bytes=1000).collect(_issue(), [], issue_repo=REPO)
	assert not any(item["reason"] == "cached_file_missing" for item in manifest["skipped"])
	assert "(dropped: size limit)" in (tmp_path / "evidence/INDEX.md").read_text()
	assert not any("/actions/jobs/" in path for path in second.paths)


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


def test_github_delete_accepts_empty_204_and_rejects_failed_response() -> None:
	commands = []

	def runner(command, **_kwargs):
		commands.append(command)
		return subprocess.CompletedProcess(command, 0 if len(commands) == 1 else 403, b"", b"forbidden")

	gh = ev.GitHub(runner, sleep=lambda _seconds: None)
	assert gh.delete(f"repos/{REPO}/actions/caches/10") is True
	assert gh.delete(f"repos/{REPO}/actions/caches/11") is False
	assert commands == [
		["gh", "api", "-X", "DELETE", f"repos/{REPO}/actions/caches/10"],
		["gh", "api", "-X", "DELETE", f"repos/{REPO}/actions/caches/11"],
	]


def test_purge_legacy_evidence_caches_filters_keys_and_continues_after_delete_failure() -> None:
	commands = []
	cache_list_path = f"repos/{REPO}/actions/caches?key=heal-evidence-&per_page=100"
	listing = {"actions_caches": [
		{"key": "heal-evidence-1-2-3", "id": 1},
		{"key": "heal-evidence-4-5-6", "id": 2},
		{"key": "heal-evidence-7-8-9", "id": 3},
		{"key": "heal-evidence-x", "id": 4},
		{"key": "heal-evidence-1-2-3-extra", "id": 5},
		{"key": "some-other-cache", "id": 6},
		{"key": "heal-evidence-1-2-3", "id": True},
		{"key": "heal-evidence-1-2-3", "id": 0},
	]}

	def runner(command, **_kwargs):
		commands.append(command)
		if command[-1] == cache_list_path:
			return subprocess.CompletedProcess(command, 0, json.dumps(listing).encode(), b"")
		return subprocess.CompletedProcess(command, 403 if command[-1].endswith("/2") else 0, b"", b"forbidden")

	gh = ev.GitHub(runner, sleep=lambda _seconds: None)
	assert ev.purge_legacy_evidence_caches(gh, REPO) == {
		"listed": 8, "matched": 3, "deleted": 2, "failed": 1, "status": "ok",
	}
	assert commands == [["gh", "api", cache_list_path]] + [
		["gh", "api", "-X", "DELETE", f"repos/{REPO}/actions/caches/{cache_id}"]
		for cache_id in (1, 2, 3)
	]


@pytest.mark.parametrize("list_response,status,listed", [
	(None, "list_failed", 0),
	(b'{}', "list_failed", 0),
	(b'{"actions_caches":[]}', "none", 0),
])
def test_purge_legacy_evidence_caches_handles_unavailable_and_empty_list(list_response, status, listed) -> None:
	commands = []

	def runner(command, **_kwargs):
		commands.append(command)
		return subprocess.CompletedProcess(command, 0 if list_response is not None else 403, list_response or b"", b"forbidden")

	gh = ev.GitHub(runner, sleep=lambda _seconds: None)
	assert ev.purge_legacy_evidence_caches(gh, REPO) == {
		"listed": listed, "matched": 0, "deleted": 0, "failed": 0, "status": status,
	}
	assert commands == [["gh", "api", f"repos/{REPO}/actions/caches?key=heal-evidence-&per_page=100"]]


def test_purge_legacy_cache_cli_prints_json_and_exits_zero(monkeypatch, capsys) -> None:
	class StubGitHub:
		def json(self, _path):
			return {"actions_caches": []}

	monkeypatch.setattr(ev, "GitHub", StubGitHub)
	assert ev.main(["purge-legacy-cache", "--repo", REPO]) == 0
	assert json.loads(capsys.readouterr().out) == {
		"listed": 0, "matched": 0, "deleted": 0, "failed": 0, "status": "none",
	}
	assert ev.purge_legacy_evidence_caches(StubGitHub(), "not a slug")["status"] == "invalid_repo"


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
	names = ["Gate workflow-heal evidence", "Collect workflow-heal evidence"]
	positions = [text.index(f"      - name: {name}\n") for name in names]
	assert positions == sorted(positions) and positions[-1] < text.index(f"      - name: {agent_step}\n")
	assert "Restore workflow-heal evidence cache" not in text
	assert "Save workflow-heal evidence cache" not in text
	assert "runner.temp }}/heal-evidence" not in text
	for name in names:
		step = _step(text, name)
		# Fail open: a broken evidence step never blocks the stage.
		assert "continue-on-error: true" in step, (workflow, name)
	gate = _step(text, "Gate workflow-heal evidence")
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in gate
	assert 'purge-legacy-cache --repo "${GITHUB_REPOSITORY}" || true' in gate
	assert 'if [ "${enabled}" = "true" ]; then' in gate
	collect = _step(text, "Collect workflow-heal evidence")
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in collect
	assert f"--display-root {display_root}" in collect
	assert '--issue-json "${ISSUE_META_FILE}"' in collect and "--comments-json" in collect
	assert 'echo "HEAL_EVIDENCE_DIR=${evidence_dir}" >> "$GITHUB_ENV"' in collect
	assert "workflow_failure_heal_evidence.py eligible" in gate
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
	structured_call = 'workflow_failure_heal_evidence.py prompt-section --format structured --evidence-dir "${HEAL_EVIDENCE_DIR}"'
	clarify = _step((WORKFLOWS / "clarify.yml").read_text(encoding="utf-8"), "Run Codex")
	assert call in clarify and 'export CLARIFY_EVIDENCE_DIR="${HEAL_EVIDENCE_DIR:-}"' in clarify
	assert clarify.index('cat "${ISSUE_CONTEXT_FILE}"') < clarify.index(call) < clarify.index('} > "${CODEX_PROMPT_FILE}"')
	implement = _step((WORKFLOWS / "implement.yml").read_text(encoding="utf-8"), "Run Codex implementation")
	assert implement.index('cat "${IMPLEMENTATION_CONTEXT_FILE}"') < implement.index(structured_call)
	plan = (REPO_ROOT / "scripts" / "run_plan_codex.sh").read_text(encoding="utf-8")
	assert plan.index('cat "${PLANNING_CONTEXT_FILE}"') < plan.index(structured_call) < plan.index('} > "${CODEX_PROMPT_FILE}"')


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
