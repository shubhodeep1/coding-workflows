#!/usr/bin/env python3
"""Contract tests for review-pipeline plumbing in review_autofix.yml.

This issue only wires existing floor/consolidator/parser/ledger controls and
adds a local-artifact-only step-summary block; the broader autofix flow must
remain unchanged.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _step_block(step_name: str) -> str:
	lines = _workflow_text().splitlines()
	needle = f"- name: {step_name}"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		step_indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:"):
				indent = len(candidate) - len(candidate.lstrip(" "))
				if indent == step_indent:
					end = j
					break
		return "\n".join(lines[idx:end])
	raise AssertionError(f"Step not found in workflow: {step_name}")


def test_review_pipeline_knobs_are_wired_into_codex_agent_env() -> None:
	workflow = _workflow_text()
	for expected in (
		"REVIEW_FLOOR_RULES_ENABLED: ${{ vars.REVIEW_FLOOR_RULES_ENABLED || '1' }}",
		"REVIEW_FLOOR_KEYWORDS_FILE: ${{ vars.REVIEW_FLOOR_KEYWORDS_FILE || '' }}",
		"REVIEW_CONSOLIDATOR_ENABLED: ${{ vars.REVIEW_CONSOLIDATOR_ENABLED || '1' }}",
		"REVIEW_CONSOLIDATOR_MODEL: ${{ vars.REVIEW_CONSOLIDATOR_MODEL || 'openai/gpt-5.4' }}",
		"REVIEW_CONSOLIDATOR_REASONING: ${{ vars.REVIEW_CONSOLIDATOR_REASONING || 'xhigh' }}",
		"REVIEW_CONSOLIDATOR_TIMEOUT_SECS: ${{ vars.REVIEW_CONSOLIDATOR_TIMEOUT_SECS || '300' }}",
		"REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT: ${{ vars.REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT || '16000' }}",
		"REVIEW_PARSER_FAILOPEN: ${{ vars.REVIEW_PARSER_FAILOPEN || '1' }}",
		"REVIEW_LEDGER_ENABLED: ${{ vars.REVIEW_LEDGER_ENABLED || '1' }}",
		"REVIEW_LEDGER_PERSIST_LIMIT: ${{ vars.REVIEW_LEDGER_PERSIST_LIMIT || '2' }}",
		"REVIEW_REVIEWER_CHECKLIST_ENABLED: ${{ vars.REVIEW_REVIEWER_CHECKLIST_ENABLED || '1' }}",
		"REVIEW_REVIEWER_ITERATION_SCOPING: ${{ vars.REVIEW_REVIEWER_ITERATION_SCOPING || '1' }}",
	):
		assert expected in workflow, f"Missing codex-agent env wiring: {expected}"


def test_review_pipeline_summary_step_is_local_only_and_grep_friendly() -> None:
	block = _step_block("Append review pipeline iteration summary")
	assert "### Review Pipeline — Iteration ${iteration_label}" in block
	assert "reviewer_scope_label=\"full-diff\"" in block, (
		"Summary step must not overclaim scoped reviewer behaviour before "
		"review_run_reviewers.sh consumes REVIEW_REVIEWER_ITERATION_SCOPING"
	)
	for expected in (
		"| Reviewers run | ${reviewers_run} |",
		"| Reviewer scope | ${reviewer_scope_label} |",
		"| Raw bundle size (bytes) | ${bundle_bytes} |",
		"| Floor tags | ${floor_tag_count} |",
		"| Consolidator model | ${REVIEW_CONSOLIDATOR_MODEL:-openai/gpt-5.4} |",
		"| Consolidator invoked | ${consolidator_invoked} |",
		"| Consolidator output bytes | ${consolidator_output_bytes} |",
		"| Parsed issue blocks | ${parsed_blocks} |",
		"| Passthrough blocks | ${passthrough_blocks} |",
		"| Line-unverified blocks | ${line_unverified} |",
		"| Ledger entries total | ${ledger_total} |",
		"| NEW | ${ledger_new} |",
		"| PERSISTING | ${ledger_persisting} |",
		"| FIXED | ${ledger_fixed} |",
		"| RESURGENT | ${ledger_resurgent} |",
		"| accepted-residual | ${ledger_accepted_residual} |",
		"| Editor invoked | ${editor_invoked} |",
		"| CONSOLIDATOR_OVERRIDDEN count | ${override_count} |",
		"| Editor commit produced | ${editor_commit_produced} |",
	):
		assert expected in block, f"Missing summary row contract: {expected}"
	for artifact in (
		"${RUNTIME_DIR}/reviewer_bundle.txt",
		"${RUNTIME_DIR}/floor_tags.txt",
		"${RUNTIME_DIR}/consolidator_raw.txt",
		"${RUNTIME_DIR}/parser_stats.txt",
		"${RUNTIME_DIR}/ledger_status.txt",
		"grep -c 'CONSOLIDATOR_OVERRIDDEN:' \"${EDITOR_SUMMARY_FILE}\"",
		"EDITOR_COMMIT_PRODUCED: ${{ steps.commit_changes.outputs.did_commit }}",
	):
		assert artifact in block, f"Summary step is missing local metric source: {artifact}"
	assert "gh api" not in block
	assert "gh_retry" not in block
	assert "curl https://api.github.com" not in block


def main() -> int:
	test_review_pipeline_knobs_are_wired_into_codex_agent_env()
	test_review_pipeline_summary_step_is_local_only_and_grep_friendly()
	print("OK: review_autofix review-pipeline plumbing contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
