#!/usr/bin/env python3
"""Regression checks for the consumer-facing clarify.yml job trigger guard.

Consumers pin ``clarify.yml@stable`` and delegate every ``issues.opened`` /
``issue_comment`` event to it. Central, main-run producers (the retro fan-out
in ``workflow-log-analysis.yml`` and the security-audit fan-out) create
informational tracker issues in every consumer, labeled ``ai:retro`` /
``ai:security-audit`` at creation time. Those issues must never enter the
clarify pipeline — otherwise they get an ``ai:clarification`` label and a
stall marker and nag forever.

The clarify job ``if:`` guard is the only thing standing between those
labeled issues and a clarify run, so it must exclude every fan-out label.
This test pins that invariant so a future edit (or a stale ``stable`` that
drifted from ``main``) cannot silently drop an exclusion.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CLARIFY_WF = REPO_ROOT / ".github" / "workflows" / "clarify.yml"

# Labels applied at issue-creation time by central fan-out producers. An issue
# carrying any of these is informational and must be skipped by the clarify
# job's issues.opened branch.
EXCLUDED_OPEN_LABELS = (
	"ai:orchestrator-tracking",
	"ai:security-audit",
	"ai:retro",
)


def _clarify_if_guard() -> str:
	"""Return the folded ``if:`` expression of the ``clarify`` job."""
	text = CLARIFY_WF.read_text(encoding="utf-8")
	# The guard is a folded scalar: `if: >-` followed by indented lines up to
	# the next same-or-lower-indent key (`runs-on:`).
	match = re.search(
		r"\n    if: >-\n(?P<body>(?:      .*\n)+)",
		text,
	)
	assert match, "could not locate the clarify job `if: >-` guard in clarify.yml"
	return match.group("body")


def test_clarify_open_guard_excludes_all_fanout_labels() -> None:
	guard = _clarify_if_guard()
	for label in EXCLUDED_OPEN_LABELS:
		needle = f"!contains(toJson(github.event.issue.labels.*.name), '{label}')"
		assert needle in guard, (
			f"clarify.yml job guard must exclude '{label}'-labeled issues from "
			f"the issues.opened branch (missing: {needle}). Fan-out tracker "
			f"issues would otherwise trigger a consumer clarify run and stall."
		)


def test_clarify_open_branch_still_gates_on_opened_action() -> None:
	# Guard must still only fire on the `opened` action for the issues branch,
	# so re-labeling an existing tracker (issues.labeled) never triggers clarify.
	guard = _clarify_if_guard()
	assert "github.event_name == 'issues'" in guard
	assert "github.event.action == 'opened'" in guard


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
