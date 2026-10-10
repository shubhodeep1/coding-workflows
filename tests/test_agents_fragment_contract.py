#!/usr/bin/env python3
"""Wiring for agents.d/ fragments (CLAUDE.md §30): fold, check, and the readers.

Direct-run like tests/test_changelog_fragment_contract.py; pytest collects it too.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RELEASE_WORKFLOWS = (WORKFLOWS / "mark-stable.yml", WORKFLOWS / "test-and-mark-stable.yml")
UPDATE_WORKFLOWS = WORKFLOWS / "update_workflows.yml"
CI = WORKFLOWS / "ci.yml"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _step(workflow: str, name: str) -> str:
	return workflow.split(f"name: {name}", 1)[1].split("\n      - name: ", 1)[0]


def test_release_jobs_fold_agents_fragments_in_the_changelog_commit() -> None:
	for path in RELEASE_WORKFLOWS:
		step = _step(_read(path), "Assemble changelog fragments")
		assert "scripts/assemble_agents.py assemble --repo-root ." in step, path.name
		assert "|| echo \"::warning::agents.md fragment assembly failed" in step, f"{path.name}: must fail open"
		assert "git status --porcelain -- CHANGELOG.md changelog.d agents.md agents.d" in step
		assert "git add -A -- CHANGELOG.md changelog.d agents.md agents.d" in step


def test_release_tag_guard_accepts_the_agents_fold() -> None:
	for path in RELEASE_WORKFLOWS:
		workflow = _read(path)
		assert "grep -vE '^(CHANGELOG\\.md$|changelog\\.d/|agents\\.md$|agents\\.d/)'" in workflow, path.name
		assert "grep -vE '^(CHANGELOG\\.md$|changelog\\.d/)'" not in workflow


def test_consumer_sync_folds_agents_fragments_and_reports_them() -> None:
	workflow = _read(UPDATE_WORKFLOWS)
	step = _step(workflow, "Assemble agents.md fragments")
	assert "id: agents_assemble" in step
	assert 'AGENTS_ASSEMBLER="${UPSTREAM_SCRIPTS_DIR}/assemble_agents.py"' in step
	assert 'echo "Upstream has no scripts/assemble_agents.py at this ref' in step
	assert workflow.index("name: Assemble changelog fragments") < workflow.index("name: Assemble agents.md fragments") < workflow.index("name: Compare and update workflow wrappers")
	commit_step = _step(workflow, "Commit and push updates")
	assert 'git add -A -- "${{ steps.agents_assemble.outputs.agents_file }}" agents.d' in commit_step
	assert "agents.d fragments assembled into" in commit_step
	notify = _step(workflow, "Send Telegram notification")
	assert "agents.d fragments assembled into ${AGENTS_FILE}: ${AGENTS_FRAGMENT_COUNT} fragment(s)." in notify


def test_ci_checks_fragments_and_runs_the_assembler_tests() -> None:
	ci = _read(CI)
	assert "python3 scripts/assemble_agents.py check --repo-root ." in ci
	assert "tests/test_assemble_agents.py" in ci
	assert "tests/test_agents_fragment_contract.py" in ci


def test_pending_fragments_fold_into_this_repository() -> None:
	result = subprocess.run(
		[sys.executable, str(REPO_ROOT / "scripts" / "assemble_agents.py"), "check", "--repo-root", str(REPO_ROOT)],
		capture_output=True, text=True, check=False, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
	)
	assert result.returncode == 0, result.stdout + result.stderr


def test_instructions_point_at_fragments() -> None:
	claude = _read(REPO_ROOT / "CLAUDE.md")
	assert "## §30. agents.md Fragments (MANDATORY)" in claude
	assert "agents.d/<issue-or-pr>-<slug>.md" in claude
	assert _read(REPO_ROOT / "workflow-templates" / "CLAUDE.md") == claude
	for prompt in ("prompts/mode-implement.txt", "prompts/_templates/mode-implement.txt"):
		text = _read(REPO_ROOT / prompt)
		assert "agents.d/<issue-or-pr>-<slug>.md" in text, prompt
		assert "never edit `agents.md` directly" in text, prompt


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"OK: {len(tests)} agents fragment contract tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
