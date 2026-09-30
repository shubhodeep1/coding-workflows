#!/usr/bin/env python3
"""Guard every workflow file against GitHub's 512,000-byte workflow size limit.

GitHub does not start runs for a workflow file over 500 KiB (512,000 bytes;
measured on 2026-09-24: 512,000 runs, 512,001 does not). It does not report
an error either: every push creates a zero-job run named after the file path
that concludes ``failure`` with "This run likely failed because of a workflow
file issue", and a reusable workflow over the limit cannot be called. #4327
pushed ``.github/workflows/review_autofix.yml`` from 505,283 to 540,537
bytes and the phantom runs broke the stable release gate (run 35903885958).

The guard fails at 480,000 bytes, 32,000 bytes before the hard limit, so the
split lands in a reviewed PR instead of after the workflow has stopped
running. Fix a failure by moving large inline ``run:`` bodies into
``scripts/`` (see the "Workflow file size limit" section in agents.md), not
by raising the threshold.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
GITHUB_WORKFLOW_FILE_SIZE_LIMIT_BYTES = 512_000
WORKFLOW_FILE_SIZE_GUARD_BYTES = 480_000


def _workflow_files() -> list[Path]:
	return sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])


def test_guard_threshold_leaves_headroom_below_github_limit() -> None:
	assert WORKFLOW_FILE_SIZE_GUARD_BYTES < GITHUB_WORKFLOW_FILE_SIZE_LIMIT_BYTES
	assert GITHUB_WORKFLOW_FILE_SIZE_LIMIT_BYTES == 500 * 1024


def test_every_workflow_file_is_below_size_guard() -> None:
	files = _workflow_files()
	assert files, f"No workflow files found under {WORKFLOWS_DIR}"
	oversized = [
		(path.relative_to(REPO_ROOT).as_posix(), path.stat().st_size)
		for path in files
		if path.stat().st_size >= WORKFLOW_FILE_SIZE_GUARD_BYTES
	]
	assert not oversized, (
		"Workflow file(s) at or above the "
		f"{WORKFLOW_FILE_SIZE_GUARD_BYTES:,}-byte guard (GitHub stops starting runs above "
		f"{GITHUB_WORKFLOW_FILE_SIZE_LIMIT_BYTES:,} bytes): "
		+ ", ".join(f"{name}={size:,} bytes" for name, size in oversized)
		+ ". Move the largest inline run: bodies into scripts/ in this PR "
		"(agents.md, 'Workflow file size limit'); do not raise the guard."
	)


def main() -> int:
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	for func in test_functions:
		func()
	largest = max(_workflow_files(), key=lambda path: path.stat().st_size)
	print(
		f"OK: {len(test_functions)} workflow file size checks passed "
		f"(largest {largest.name}={largest.stat().st_size:,} bytes, guard {WORKFLOW_FILE_SIZE_GUARD_BYTES:,})"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
