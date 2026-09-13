#!/usr/bin/env python3
"""Pin that the review-blocked judge does not re-post its assessment when
a pending approval request is reused.

Background
----------
scripts/review_rb_judge.sh looks for an unconsumed REVIEW_BLOCKED_APPROVAL_V1
request bound to the current head before it consults the model.  When one
exists, RB_PENDING_DECISION_JSON is populated and the model call is skipped
("Reusing pending review-blocked approval request; skipping a new model
decision.").  The decision is therefore byte-for-byte the one already
assessed on the PR when the request was created.

Before this fix the script still ran post_review_blocked_assessment on that
reuse path, so every review re-dispatch (the 30-minute review sweep re-runs
the judge until a trusted human approves) added another identical
"## Review-Blocked Judge Decision" comment: PR #4079 collected 22 of them in
eleven hours.  The poller's own review-blocked rung in
scripts/orchestrate_poll_process.sh already guards its assessment post on an
empty RB_PENDING_APPROVAL_REQUEST; the judge script now mirrors that.

The contract
------------
Between the "Post judge assessment to PR" section marker and the
RB_APPROVAL_REQUEST_ID reset, the only call to post_review_blocked_assessment
must sit inside the else-branch of an `if [ -n "${RB_PENDING_DECISION_JSON}" ]`
guard, and the reuse branch must log why the post is skipped.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RB_JUDGE_SCRIPT = REPO_ROOT / "scripts" / "review_rb_judge.sh"


def _rb_judge_text() -> str:
	return RB_JUDGE_SCRIPT.read_text(encoding="utf-8")


def _assessment_block() -> str:
	src = _rb_judge_text()
	start = src.index("# Post judge assessment to PR")
	end = src.index('RB_APPROVAL_REQUEST_ID=""', start)
	block = src[start:end]
	assert block.count("post_review_blocked_assessment") == 1, (
		"expected exactly one assessment post between the section marker "
		"and the approval-request handling"
	)
	return block


def test_assessment_post_is_guarded_by_pending_decision_reuse() -> None:
	block = _assessment_block()
	guard = 'if [ -n "${RB_PENDING_DECISION_JSON}" ]; then'
	assert guard in block, "assessment post must be gated on RB_PENDING_DECISION_JSON"
	guard_idx = block.index(guard)
	else_idx = block.index("\nelse\n", guard_idx)
	post_idx = block.index("post_review_blocked_assessment", guard_idx)
	fi_idx = block.index("\nfi\n", else_idx)
	assert guard_idx < else_idx < post_idx < fi_idx, (
		"post_review_blocked_assessment must live in the else-branch of the "
		"RB_PENDING_DECISION_JSON guard so a reused pending request posts nothing"
	)


def test_reuse_branch_logs_skip_reason() -> None:
	block = _assessment_block()
	assert re.search(
		r'if \[ -n "\$\{RB_PENDING_DECISION_JSON\}" \]; then\n\s+echo "Pending review-blocked approval request reused; '
		r'the judge assessment was posted when the request was created, so it is not re-posted\."',
		block,
	), "the reuse branch must log why the assessment is not re-posted"


def test_reuse_path_precedes_assessment_guard() -> None:
	"""The model-skip reuse branch must set RB_PENDING_DECISION_JSON before
	the assessment guard reads it, otherwise the guard could never fire."""
	src = _rb_judge_text()
	reuse_idx = src.index(
		'echo "Reusing pending review-blocked approval request; skipping a new model decision."'
	)
	guard_idx = src.index('if [ -n "${RB_PENDING_DECISION_JSON}" ]; then', src.index("# Post judge assessment to PR"))
	assert reuse_idx < guard_idx


def main() -> int:
	import unittest

	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except unittest.SkipTest as exc:
			print(f"  SKIP  {name}: {exc}")
		except Exception as exc:  # noqa: BLE001 - report every failure
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
