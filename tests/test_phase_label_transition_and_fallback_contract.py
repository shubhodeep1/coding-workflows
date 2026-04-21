#!/usr/bin/env python3
"""Contracts for phase-label helper adoption and fallback PR payload reuse."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ISSUE_PR_STATUS_WF = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"
REVIEW_AUTOFIX_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
LABEL_HELPERS_SH = REPO_ROOT / "scripts" / "label_helpers.sh"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def test_label_helpers_exports_resilient_phase_swap_function() -> None:
	text = _read(LABEL_HELPERS_SH)
	assert "set_issue_phase_label_resilient()" in text
	assert '"ai:needs-human"' in text
	assert '"ai:blocked"' in text
	assert "::warning::GET labels failed for #${issue_number} — falling back to POST add." in text
	assert "::warning::PUT labels failed for #${issue_number} — falling back to POST add." in text
	assert "::warning::POST fallback also failed for #${issue_number}." in text


def test_issue_pr_status_uses_shared_phase_label_helper() -> None:
	text = _read(ISSUE_PR_STATUS_WF)
	assert 'source scripts/label_helpers.sh' in text
	assert 'set_issue_phase_label_resilient "${issue_number}" "${FINAL_LABEL}" "${REPOSITORY}" || true' in text
	assert '_AI_PHASE_LABELS=' not in text


def test_review_autofix_migrated_callsites_use_shared_helper() -> None:
	text = _read(REVIEW_AUTOFIX_WF)
	assert 'set_issue_phase_label_resilient "${issue_number}" "ai:ready-to-merge" "${REPOSITORY}" || true' in text
	assert 'set_issue_phase_label_resilient "${issue_number}" "ai:review-blocked" "${REPOSITORY}" || true' in text
	assert '_AI_PHASE_LABELS=' not in text


def test_fallback_parsing_prefers_event_payload_before_pr_refetch() -> None:
	issue_pr_status = _read(ISSUE_PR_STATUS_WF)
	assert 'PR_TITLE: ${{ github.event.pull_request.title || \'\' }}' in issue_pr_status
	assert 'PR_BODY: ${{ github.event.pull_request.body || \'\' }}' in issue_pr_status
	assert 'PR_DATA="$(printf \'%s %s\' "${PR_TITLE:-}" "${PR_BODY:-}")"' in issue_pr_status
	assert 'if [ -z "$(printf \'%s\' "${PR_DATA}" | tr -d \'[:space:]\')" ]; then' in issue_pr_status
	assert 'PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq \'.title + " " + (.body // "")\' 2>/dev/null || echo "")"' in issue_pr_status

	review_autofix = _read(REVIEW_AUTOFIX_WF)
	assert 'PR_TITLE: ${{ inputs.pr_title || github.event.inputs.pr_title || github.event.pull_request.title || \'\' }}' in review_autofix
	assert 'PR_BODY: ${{ inputs.pr_body || github.event.inputs.pr_body || github.event.pull_request.body || \'\' }}' in review_autofix
	assert 'PR_DATA="$(printf \'%s %s\' "${PR_TITLE:-}" "${PR_BODY:-}")"' in review_autofix
	assert 'pr_data="$(printf \'%s %s\' "${PR_TITLE:-}" "${PR_BODY:-}")"' in review_autofix
	assert 'if [ -z "$(printf \'%s\' "${PR_DATA}" | tr -d \'[:space:]\')" ]; then' in review_autofix
	assert 'if [ -z "$(printf \'%s\' "${pr_data}" | tr -d \'[:space:]\')" ]; then' in review_autofix
	assert 'PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq \'.title + " " + (.body // "")\' 2>/dev/null || echo "")"' in review_autofix
	assert 'PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq \'.title + " " + (.body // "")\' || echo "")"' in review_autofix
	assert 'pr_data="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq \'.title + " " + (.body // "")\' 2>/dev/null || echo "")"' in review_autofix


def test_new_contract_test_is_covered_by_ci() -> None:
	ci = _read(REPO_ROOT / ".github" / "workflows" / "ci.yml")
	assert "PYTHONDONTWRITEBYTECODE=1 python3 tests/test_phase_label_transition_and_fallback_contract.py" in ci


def test_review_autofix_manual_fallback_is_post_add_fail_open() -> None:
	"""When helper sourcing fails, fallback must stay fail-open with warning + POST add."""
	text = _read(REVIEW_AUTOFIX_WF)
	assert "::warning::set_issue_phase_label_resilient helper unavailable; using POST add fallback" in text
	assert "::warning::POST fallback also failed for #${issue_number}." in text


def test_migrated_issue_pr_status_has_no_manual_put_swap_block() -> None:
	text = _read(ISSUE_PR_STATUS_WF)
	assert 'gh_retry gh api -X PUT "repos/${REPOSITORY}/issues/${issue_number}/labels"' not in text


if __name__ == "__main__":
	test_label_helpers_exports_resilient_phase_swap_function()
	test_issue_pr_status_uses_shared_phase_label_helper()
	test_review_autofix_migrated_callsites_use_shared_helper()
	test_fallback_parsing_prefers_event_payload_before_pr_refetch()
	test_new_contract_test_is_covered_by_ci()
	test_review_autofix_manual_fallback_is_post_add_fail_open()
	test_migrated_issue_pr_status_has_no_manual_put_swap_block()
	print("PASS")
