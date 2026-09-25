#!/usr/bin/env python3
"""Phase 4 of test-and-mark-stable.yml must ignore "workflow file issue" runs.

When GitHub cannot load a workflow (for example a file over the 512,000-byte
limit), every push creates a zero-job run named after the file path, such as
``.github/workflows/review_autofix.yml``, that concludes ``failure``. The name
matched Phase 4's ``Review|review|autofix|self-healing`` regex and the run
sat on the pinned head_sha, so Phase 4 accepted it instantly as "completed
with failure" before any editor ran, and Phase 4b then failed with
``retry_timeout`` (stable gate run 35903885958). Every workflow here sets
``name:``, so ``.name == .path`` identifies such runs without an extra API
call.

The filters are taken from the workflow and expanded by bash exactly as the
step expands them, then run through jq against fixture run lists.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_AND_MARK_STABLE = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TEMPLATES_DIR = REPO_ROOT / "workflow-templates"

PHANTOM_RUN = {
	"id": 35938372361,
	"name": ".github/workflows/review_autofix.yml",
	"path": ".github/workflows/review_autofix.yml",
	"event": "push",
	"status": "completed",
	"conclusion": "failure",
	"head_sha": "pinsha",
	"created_at": "2026-09-24T00:25:42Z",
	"updated_at": "2026-09-24T00:25:43Z",
}
REAL_REVIEW_RUN = {
	"id": 35938372000,
	"name": "Internal Review",
	"path": ".github/workflows/internal-review.yml",
	"event": "pull_request",
	"status": "in_progress",
	"conclusion": None,
	"head_sha": "pinsha",
	"created_at": "2026-09-24T00:25:40Z",
	"updated_at": "2026-09-24T00:26:00Z",
}


def _workflow_text() -> str:
	return TEST_AND_MARK_STABLE.read_text(encoding="utf-8")


def _pinned_filter() -> str:
	"""The head_sha-pinned filter (double-quoted in the step), bash-expanded."""
	match = re.search(r'--jq ("\[\.workflow_runs\[\] \| select\(\.name \| test\(\\"Review\|.*?")\s*\|\|', _workflow_text())
	assert match, "Pinned Phase 4 review-run --jq filter not found in test-and-mark-stable.yml"
	result = subprocess.run(
		["bash", "-c", f'PIN_SHA=pinsha; BAIT_CREATED_AT=""; printf "%s" {match.group(1)}'],
		capture_output=True,
		text=True,
		check=True,
	)
	return result.stdout


def _pull_request_filter() -> str:
	"""The fallback event=pull_request filter (single-quoted, literal)."""
	match = re.search(r"--jq '(\[\.workflow_runs\[\] \| select\(\.name \| test\(\"Review\|[^']*)'", _workflow_text())
	assert match, "Fallback Phase 4 review-run --jq filter not found in test-and-mark-stable.yml"
	return match.group(1)


def _jq(program: str, runs: list[dict]) -> object:
	jq = shutil.which("jq")
	assert jq, "jq is required for this test"
	result = subprocess.run(
		[jq, "-c", program],
		input=json.dumps({"workflow_runs": runs}),
		capture_output=True,
		text=True,
		check=True,
	)
	return json.loads(result.stdout)


def test_both_phase4_filters_drop_runs_named_after_their_path() -> None:
	assert _pinned_filter().count("select(.name != .path)") == 1
	assert _pull_request_filter().count("select(.name != .path)") == 1


def test_pinned_filter_prefers_real_review_run_over_phantom() -> None:
	chosen = _jq(_pinned_filter(), [PHANTOM_RUN, REAL_REVIEW_RUN])
	assert isinstance(chosen, dict) and chosen["id"] == REAL_REVIEW_RUN["id"], chosen


def test_pinned_filter_returns_null_when_only_phantom_exists() -> None:
	assert _jq(_pinned_filter(), [PHANTOM_RUN]) is None


def test_pull_request_filter_skips_phantom() -> None:
	chosen = _jq(_pull_request_filter(), [REAL_REVIEW_RUN, {**PHANTOM_RUN, "event": "pull_request"}])
	assert isinstance(chosen, dict) and chosen["id"] == REAL_REVIEW_RUN["id"], chosen


def test_every_workflow_and_template_sets_a_name() -> None:
	# The .name != .path check is only sound while no workflow falls back
	# to its path as the run name.
	files = sorted([*WORKFLOWS_DIR.glob("*.yml"), *TEMPLATES_DIR.glob("*.yml")])
	assert files
	unnamed = [
		path.relative_to(REPO_ROOT).as_posix()
		for path in files
		if not re.search(r"^name:\s*\S", path.read_text(encoding="utf-8"), re.MULTILINE)
	]
	assert not unnamed, f"Workflows without a top-level name: {unnamed}"


def main() -> int:
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	for func in test_functions:
		func()
	print(f"OK: {len(test_functions)} Phase 4 phantom review-run filter checks passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
