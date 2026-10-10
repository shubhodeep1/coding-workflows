#!/usr/bin/env python3
"""Every test file must run in some workflow.

On 2026-10-10, 66 of the 315 `tests/test_*` files were referenced by no
workflow, so CI never ran them. Seven of them had gone stale against the
code they covered: the resolver had become sandbox-only, Codex had moved into
the isolated container (#6187), a staged block had grown, and the overlay
root had moved under RUNNER_TEMP. Two more spent about 35 seconds per test
fetching the ai-memory branch from origin. Nobody noticed, because nothing
ran them.

This guard fails when a `tests/test_*.py` or `tests/test_*.sh` file is named
in no `.github/workflows/*.yml` file. A file that cannot run in CI goes in
UNWIRED_ALLOWLIST with the reason; a stale allowlist entry fails too.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# "tests/<file>": "why it cannot run in CI". Keep it empty when possible.
UNWIRED_ALLOWLIST: dict[str, str] = {}


def _workflow_text() -> str:
	"""All workflow text minus full-line YAML comments, so a path named only in a comment does not count as wired."""
	return "\n".join(
		line
		for path in sorted(WORKFLOWS_DIR.glob("*.yml"))
		for line in path.read_text(encoding="utf-8").splitlines()
		if not line.lstrip().startswith("#")
	)


def _test_files() -> list[str]:
	return sorted(
		path.relative_to(ROOT).as_posix()
		for path in (ROOT / "tests").glob("test_*")
		if path.is_file() and path.suffix in (".py", ".sh")
	)


def _is_wired(relative: str, workflow_text: str) -> bool:
	return relative in workflow_text


def test_every_test_file_runs_in_a_workflow() -> None:
	workflow_text = _workflow_text()
	unwired = [relative for relative in _test_files() if relative not in UNWIRED_ALLOWLIST and not _is_wired(relative, workflow_text)]
	assert not unwired, (
		"These test files run in no workflow; add each to a CI step in .github/workflows/ci.yml "
		"(or to UNWIRED_ALLOWLIST with the reason it cannot run there):\n  " + "\n  ".join(unwired)
	)


def test_allowlist_entries_are_real_unwired_files_with_reasons() -> None:
	workflow_text = _workflow_text()
	existing = set(_test_files())
	for relative, reason in UNWIRED_ALLOWLIST.items():
		assert relative in existing, f"{relative} is allowlisted but does not exist"
		assert reason.strip(), f"{relative} is allowlisted without a reason"
		assert not _is_wired(relative, workflow_text), f"{relative} is allowlisted but a workflow runs it; drop the entry"


def test_the_guard_sees_the_test_tree() -> None:
	files = _test_files()
	assert len(files) > 100
	assert "tests/test_ci_wires_every_test_file.py" in files
	assert _is_wired("tests/test_ci_wires_every_test_file.py", _workflow_text())
