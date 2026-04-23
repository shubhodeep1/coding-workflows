#!/usr/bin/env python3
"""Contract tests for payload-first fallback in issue_pr_status workflow."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def test_payload_first_fallback_and_shared_helper_usage() -> None:
	text = _workflow_text()

	assert "PR_TITLE: ${{ github.event.pull_request.title }}" in text
	assert "PR_BODY: ${{ github.event.pull_request.body || '' }}" in text
	assert 'PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"' in text
	assert 'set_issue_phase_label_resilient "${issue_number}" "${FINAL_LABEL}" "${REPOSITORY}"' in text
	assert "_AI_PHASE_LABELS='[\"ai:done\"" not in text

	payload_pos = text.find('PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"')
	fetch_pos = text.find('PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq')
	assert payload_pos != -1
	assert fetch_pos != -1
	assert payload_pos < fetch_pos, "Fallback GH pull fetch must only run after payload text check"


def test_fallback_regex_drops_bare_mentions_keeps_closing_keywords_and_urls() -> None:
	"""Regression guard for issue #1469.

	The previous fallback regex matched bare prose like ``issue #1469`` and
	bare paths like ``issues/1469``, so any PR body merely *mentioning* an
	issue would be treated as a closing link by this workflow. That caused
	orchestrator tracking issues to be wrongly labeled ai:merged and
	auto-closed when a sub-issue PR's body referenced them in passing.

	This test pins the tightened regex to GitHub closing-keyword semantics
	plus full repo-scoped URLs/paths, and forbids re-introducing the two
	bare-mention patterns.
	"""
	text = _workflow_text()

	assert (
		r'(github\\.com/${REPOSITORY_ESCAPED}/issues/[0-9]+|'
		r'${REPOSITORY_ESCAPED}/issues/[0-9]+|'
		r'(closes|fixes|resolves)[[:space:]]*:?[[:space:]]*#[[:space:]]*[0-9]+)'
	) in text, "Tightened fallback regex must be present verbatim"

	# Forbidden: bare prose mentions that triggered the original bug.
	assert 'issue[[:space:]]*#[[:space:]]*[0-9]+' not in text, (
		"Bare 'issue #N' fallback pattern must not be re-introduced — it caused "
		"incorrect ai:merged labeling on issue #1469."
	)
	assert '(^|[^[:alnum:]_/-])issues/[0-9]+' not in text, (
		"Bare 'issues/N' fallback pattern must not be re-introduced — it caused "
		"incorrect ai:merged labeling on issues mentioned in PR prose."
	)


def test_orchestrator_tracking_issues_are_skipped_in_label_close_loop() -> None:
	"""Regression guard for issue #1469.

	When a PR's body mentions an orchestrator tracking issue (or one is
	resolved via the regex fallback), this workflow must not relabel or
	auto-close it. Terminal-phase ownership for orchestrator-managed
	tracking issues belongs exclusively to scripts/orchestrate_poll_process.sh.

	The Telegram alert step at the bottom of the workflow already had an
	IS_ORCHESTRATED guard scanning the issue body for the
	"Managed by: AI Orchestrator" marker, but the guard was not applied
	to the label/close path above it. This test pins the new guard.
	"""
	text = _workflow_text()

	# The detection step builds an aliased GraphQL query that fetches both
	# labels and body for every linked issue in a single API call.
	assert "ORCH_ALIAS_FRAGMENT" in text, (
		"Expected batched GraphQL detection of orchestrator-managed issues"
	)
	assert ' i${ORCH_IDX}: issue(number: ${_orch_num}) { number labels(first: 50) { nodes { name } } body }' in text, (
		"Expected aliased GraphQL fragment that fetches labels+body in one call"
	)

	# Detection must check both the orchestrator-tracking label and the
	# body marker (either signal must be sufficient).
	assert 'index("ai:orchestrator-tracking")' in text
	assert 'contains("Managed by: AI Orchestrator")' in text

	# Fail-open contract: GraphQL failure must fall back to per-issue REST,
	# not silently degrade to the buggy "label everything" behavior.
	assert "Orchestrator-issue batch detection failed; falling back to per-issue REST" in text, (
		"GraphQL failure path must use per-issue REST fallback so a transient "
		"GraphQL error cannot re-introduce the auto-label bug."
	)

	# Loop guard: the label/close loop must consult ORCHESTRATED_ISSUES
	# and continue (skip) before touching the label or issue state.
	assert (
		'if [ -n "${ORCHESTRATED_ISSUES}" ] && '
		'printf \'%s\\n\' "${ORCHESTRATED_ISSUES}" | grep -qxF "${issue_number}"; then'
	) in text, "Expected ORCHESTRATED_ISSUES gate inside the label/close loop"

	# Detection block must precede the label/close loop.
	detect_pos = text.find("ORCH_ALIAS_FRAGMENT=\"\"")
	gate_pos = text.find('Skipping orchestrator-managed issue #${issue_number}')
	label_call_pos = text.find('set_issue_phase_label_resilient "${issue_number}" "${FINAL_LABEL}" "${REPOSITORY}"')
	close_call_pos = text.find('gh_retry gh issue close "${issue_number}" -R "${REPOSITORY}"')
	assert detect_pos != -1, "Detection block missing"
	assert gate_pos != -1, "Skip-log marker missing inside label/close loop"
	assert label_call_pos != -1, "set_issue_phase_label_resilient call missing"
	assert close_call_pos != -1, "gh issue close call missing"
	assert detect_pos < gate_pos < label_call_pos, (
		"Orchestrator detection must run before, and the skip gate must precede, "
		"the label apply call."
	)
	assert gate_pos < close_call_pos, (
		"Skip gate must precede the gh issue close call."
	)


if __name__ == "__main__":
	test_payload_first_fallback_and_shared_helper_usage()
	test_fallback_regex_drops_bare_mentions_keeps_closing_keywords_and_urls()
	test_orchestrator_tracking_issues_are_skipped_in_label_close_loop()
	print("PASS")
