"""Tests for scripts/ingest_implement_plan_lessons.py and its wiring.

Covers the `## Lessons` parser, deterministic record ids, reading progress
logs at a git ref, the end-to-end write to a local `ai-memory` branch
(including idempotent re-runs and the kill switches), the
`issue_pr_status.yml` step, and the `/implement-plan-claude` +
`/verify-activation` command contracts the lessons and conformance stages
depend on.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ingest_implement_plan_lessons as ingest  # noqa: E402


SAMPLE_LOG = """# Implement-Plan Log — Sample

- Plan: docs/plans/sample-plan.md
- Status: IN_PROGRESS

## Phases
1. [x] Phase 1 — scope — PR #1 merged 2026-09-01; interventions: 0

## Lessons
- [source:conformance] A workflow reading a new repo-var must default it in both the reusable workflow and the wrapper. (files: .github/workflows/foo.yml, workflow-templates/ai-foo.yml)
- [source:validation]   Validation needs the harness image rebuilt after a Dockerfile change.
- [source:<conformance|security|validation|intervention|plan-deviation|activation>] <one reusable lesson> (files: <path>, <path>)
- [source:unknown] Unknown sources are ignored.
- plain bullet without a source tag
```
- [source:security] Lines inside a fenced block are ignored.
```

## Notes
- [source:security] Lessons outside the Lessons section are ignored.
"""


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(
		["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", *args],
		cwd=str(cwd),
		check=True,
		capture_output=True,
		text=True,
	).stdout


def test_parse_lessons_reads_only_valid_lines_in_the_lessons_section() -> None:
	lessons = ingest.parse_lessons(SAMPLE_LOG)
	assert lessons == [
		{
			"source": "conformance",
			"text": "A workflow reading a new repo-var must default it in both the reusable workflow and the wrapper.",
			"files": [".github/workflows/foo.yml", "workflow-templates/ai-foo.yml"],
		},
		{
			"source": "validation",
			"text": "Validation needs the harness image rebuilt after a Dockerfile change.",
			"files": [],
		},
	]


def test_parse_lessons_accepts_every_documented_source() -> None:
	body = "## Lessons\n" + "\n".join(f"- [source:{source}] Lesson for {source}." for source in ingest.LESSON_SOURCES)
	assert [item["source"] for item in ingest.parse_lessons(body)] == list(ingest.LESSON_SOURCES)


def test_parse_lessons_without_section_returns_nothing() -> None:
	assert ingest.parse_lessons("# Log\n\n## Notes\n- [source:security] not a lesson\n") == []


def test_parse_lessons_ignores_the_auto_decisions_section() -> None:
	# CLAUDE.md §28: auto-decisions sit next to the lessons and must never reach memory.
	markdown = (
		"# Log\n\n## Auto-decisions\n"
		"- AD-1 [phase 1/2, 2026-09-25] Which default? — Picked: A — off. Status: pending review\n"
		"- [source:plan-deviation] looks like a lesson but is under Auto-decisions\n\n"
		"## Lessons\n- [source:security] real lesson\n"
	)
	assert [item["text"] for item in ingest.parse_lessons(markdown)] == ["real lesson"]


def test_parse_lessons_preserves_parentheses_in_file_paths() -> None:
	markdown = (
		"## Lessons\n"
		"- [source:validation] Recheck routes. "
		"(files: src/routes/(login)/handler.py, tests/test_call(foo).py)\n"
	)
	assert ingest.parse_lessons(markdown) == [
		{
			"source": "validation",
			"text": "Recheck routes.",
			"files": ["src/routes/(login)/handler.py", "tests/test_call(foo).py"],
		}
	]


def test_record_id_is_deterministic_and_schema_safe() -> None:
	first = ingest.lesson_record_id("sample", "conformance", "Text.")
	assert first == ingest.lesson_record_id("sample", "conformance", "Text.")
	assert first == ingest.ai_memory_lib.make_deterministic_record_id(
		"lesson-implement-plan", "sample", "conformance", "Text."
	)
	assert first != ingest.lesson_record_id("other", "conformance", "Text.")
	assert first != ingest.lesson_record_id("sample", "security", "Text.")
	assert first.startswith("lesson-implement-plan-") and len(first) <= 128
	assert all(ch.isalnum() or ch in "_.:-" for ch in first)


def test_lesson_payload_tags_source_plan_and_files() -> None:
	payload = ingest.lesson_payload("sample", {"source": "security", "text": "T.", "files": ["a.py", "b.yml"]})
	assert payload == {
		"lesson_kind": "project_retrospective",
		"lesson_text": "T.",
		"tags": ["source:security", "plan:sample", "file:a.py", "file:b.yml"],
	}


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
	"""A clone with a bare `origin`, one committed progress log, and memory schemas on disk."""
	origin = tmp_path / "origin.git"
	_git(tmp_path, "init", "--bare", "--quiet", "-b", "main", str(origin))
	work = tmp_path / "work"
	_git(tmp_path, "clone", "--quiet", str(origin), str(work))
	log_dir = work / "docs" / "implement-plan"
	log_dir.mkdir(parents=True)
	(log_dir / "README.md").write_text("## Lessons\n- [source:security] README is never ingested.\n", encoding="utf-8")
	(log_dir / "sample.md").write_text(SAMPLE_LOG, encoding="utf-8")
	_git(work, "checkout", "--quiet", "-b", "main")
	_git(work, "add", ".")
	_git(work, "commit", "--quiet", "-m", "log")
	_git(work, "push", "--quiet", "origin", "main")
	# issue_pr_status.yml stages the memory schemas into the workspace; mirror that.
	shutil.copytree(REPO_ROOT / "ai-memory" / "schemas", work / "ai-memory" / "schemas")
	return work


def test_read_logs_at_ref_skips_readme(fixture_repo: Path) -> None:
	logs = ingest.read_logs_at_ref(fixture_repo, "HEAD", "docs/implement-plan")
	assert list(logs) == ["sample"]
	payloads, record_ids = ingest.collect_lessons(logs)
	assert len(payloads) == 2 and len(record_ids) == 2


def _args(repo: Path, **overrides: object) -> argparse.Namespace:
	values: dict[str, object] = {
		"ref": "HEAD",
		"repo_root": str(repo),
		"pr_number": 17,
		"log_dir": "docs/implement-plan",
		"memory_branch": "ai-memory",
		"memory_root": "ai-memory",
		"push_retries": 2,
		"dry_run": False,
	}
	values.update(overrides)
	return argparse.Namespace(**values)


def _memory_lessons(repo: Path) -> list[dict[str, object]]:
	listing = _git(repo, "ls-tree", "-r", "--name-only", "origin/ai-memory")
	paths = [p for p in listing.splitlines() if "/lessons_learned/" in p]
	return [json.loads(_git(repo, "show", f"origin/ai-memory:{p}")) for p in sorted(paths)]


def test_run_writes_schema_valid_records_once(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.delenv("AI_MEMORY_ENABLED", raising=False)
	monkeypatch.delenv("LESSONS_LEARNED_ENABLED", raising=False)

	first = ingest.run(_args(fixture_repo))
	assert first["parsed"] == 2 and first["written"] == 2 and first["did_push"] is True

	_git(fixture_repo, "fetch", "--quiet", "origin", "ai-memory:refs/remotes/origin/ai-memory")
	records = _memory_lessons(fixture_repo)
	assert len(records) == 2
	for record in records:
		assert record["phase"] == "implement_plan"
		assert record["lesson_kind"] == "project_retrospective"
		assert record["issue_number"] is None and record["pr_number"] == 17
		assert record["record_id"].startswith("lesson-implement-plan-")
		assert "plan:sample" in record["tags"]

	# Re-ingesting the same log (a later implement-plan PR merging) writes nothing.
	second = ingest.run(_args(fixture_repo, pr_number=18))
	assert second["parsed"] == 2 and second["written"] == 0 and second["did_push"] is False


def test_run_honours_kill_switches(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	for name in ("AI_MEMORY_ENABLED", "LESSONS_LEARNED_ENABLED"):
		monkeypatch.delenv("AI_MEMORY_ENABLED", raising=False)
		monkeypatch.delenv("LESSONS_LEARNED_ENABLED", raising=False)
		monkeypatch.setenv(name, "false")
		result = ingest.run(_args(fixture_repo))
		assert result["enabled"] is False and result["written"] == 0
	assert "ai-memory" not in _git(fixture_repo, "ls-remote", "--heads", "origin")


def test_dry_run_writes_nothing(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.delenv("AI_MEMORY_ENABLED", raising=False)
	monkeypatch.delenv("LESSONS_LEARNED_ENABLED", raising=False)
	result = ingest.run(_args(fixture_repo, dry_run=True))
	assert result["parsed"] == 2 and result["written"] == 0 and result["dry_run"] is True
	assert "ai-memory" not in _git(fixture_repo, "ls-remote", "--heads", "origin")


def test_main_fails_open_on_bad_ref(fixture_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
	assert ingest.main(["--ref", "does-not-exist", "--repo-root", str(fixture_repo)]) == 0
	err = capsys.readouterr().err
	assert "::warning::implement-plan lessons ingestion failed" in err
	assert '"fail_open": true' in err


# --- wiring contracts ---------------------------------------------------------

def _issue_pr_status_steps() -> list[dict[str, object]]:
	workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml").read_text(encoding="utf-8"))
	return workflow["jobs"]["sync-issue-status"]["steps"]


def test_issue_pr_status_wires_fail_open_ingestion_for_implement_plan_branches() -> None:
	steps = {step["name"]: step for step in _issue_pr_status_steps()}
	step = steps["Ingest implement-plan lessons into AI memory"]
	condition = str(step["if"])
	assert "github.event.pull_request.merged == true" in condition
	assert "'claude/implement-plan-'" in condition and "'claude/verify-activation-'" in condition
	assert step["continue-on-error"] is True
	env = step["env"]
	assert env["AI_MEMORY_ENABLED"] == "${{ vars.AI_MEMORY_ENABLED || 'true' }}"
	assert env["LESSONS_LEARNED_ENABLED"] == "${{ vars.LESSONS_LEARNED_ENABLED || 'true' }}"
	assert env["MERGE_COMMIT_SHA"] == "${{ github.event.pull_request.merge_commit_sha }}"
	assert "scripts/ingest_implement_plan_lessons.py" in str(step["run"])
	# Untrusted PR fields never reach the shell except through env.
	assert "${{" not in str(step["run"])

	fetch = str(steps["Fetch memory helper scripts"]["run"])
	assert '"scripts/ingest_implement_plan_lessons.py"' in fetch
	assert "lessons_learned_record.v1.json" in fetch
	# The optional helper must not gate lineage finalization.
	assert "ingest_implement_plan_lessons.py" not in fetch.split("for f in ", 1)[1].split(";", 1)[0]


def test_ingestion_runs_after_lineage_finalization() -> None:
	names = [step["name"] for step in _issue_pr_status_steps()]
	assert names.index("Ingest implement-plan lessons into AI memory") == names.index("Finalize linked issue lineage state") + 1


def test_lesson_schema_accepts_new_phase_and_kind() -> None:
	schema = json.loads((REPO_ROOT / "ai-memory" / "schemas" / "lessons_learned_record.v1.json").read_text(encoding="utf-8"))
	assert "implement_plan" in schema["properties"]["phase"]["enum"]
	assert "project_retrospective" in schema["properties"]["lesson_kind"]["enum"]
	# Existing values stay (CLAUDE.md §6).
	assert {"review_autofix", "implement", "judge"} <= set(schema["properties"]["phase"]["enum"])


COMMAND_COPIES = (
	REPO_ROOT / ".claude" / "commands",
	REPO_ROOT / "workflow-templates" / ".claude" / "commands",
)


def test_implement_plan_claude_copies_are_identical() -> None:
	source, template = (d / "implement-plan-claude.md" for d in COMMAND_COPIES)
	assert source.read_text(encoding="utf-8") == template.read_text(encoding="utf-8")


def test_implement_plan_claude_runs_conformance_before_security_and_records_lessons() -> None:
	text = (COMMAND_COPIES[0] / "implement-plan-claude.md").read_text(encoding="utf-8")
	conformance = text.index("8. **Conformance audit")
	security = text.index("9. **Security pass")
	validation = text.index("10. **Runtime validation.**")
	completion = text.index("11. **Completion PR")
	activation = text.index("12. **Verify activation.**")
	assert conformance < security < validation < completion < activation
	assert "— scope conformance" in text and "— scope activation" in text
	assert "`conformance 1/3` after the last phase" in text
	assert "3 conformance runs per project" in text
	assert "## Lessons" in text and "[source:" in text
	for source in ingest.LESSON_SOURCES:
		assert f"`{source}`" in text


@pytest.mark.parametrize("commands_dir", COMMAND_COPIES)
def test_verify_activation_documents_both_scopes(commands_dir: Path) -> None:
	text = (commands_dir / "verify-activation.md").read_text(encoding="utf-8")
	assert "## Scope" in text
	for token in ("`conformance`", "`activation`", "CONFORMANT", "Scope: full / conformance / activation"):
		assert token in text
