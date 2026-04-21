#!/usr/bin/env python3
"""Regression contract for resilient phase-label transitions helper."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_PATH = REPO_ROOT / "scripts" / "label_helpers.sh"


def _helper_text() -> str:
	return HELPER_PATH.read_text(encoding="utf-8")


def test_resilient_phase_transition_helper_uses_targeted_add_remove() -> None:
	text = _helper_text()

	assert "set_issue_phase_label_resilient()" in text
	assert '_AI_PHASE_TRANSITION_LABELS=(' in text
	assert 'gh_retry gh api -X POST "repos/${repo}/issues/${issue_number}/labels"' in text
	assert 'GH_RETRY_MAX_ATTEMPTS=1 gh_retry gh api -X DELETE "repos/${repo}/issues/${issue_number}/labels/${encoded_label}"' in text
	assert '_remove_issue_label_if_present "${issue_number}" "${phase_label}" "${repo}" || true' in text


def test_resilient_phase_transition_helper_avoids_full_label_set_put() -> None:
	text = _helper_text()
	assert 'gh_retry gh api -X PUT "repos/${repo}/issues/${issue_number}/labels"' not in text
	assert "printf '{\"labels\":%s}'" not in text


def main() -> int:
	test_resilient_phase_transition_helper_uses_targeted_add_remove()
	test_resilient_phase_transition_helper_avoids_full_label_set_put()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
