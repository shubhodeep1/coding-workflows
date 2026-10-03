#!/usr/bin/env python3
"""Contract tests for payload-first fallback in issue_pr_status workflow."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _step_script(step_name: str) -> str:
	text = _workflow_text()
	step_marker = f"      - name: {step_name}\n"
	start = text.find(step_marker)
	assert start != -1, f"Missing workflow step: {step_name}"

	run_marker = "\n        run: |\n"
	run_start = text.find(run_marker, start)
	assert run_start != -1, f"Missing run block for workflow step: {step_name}"
	run_start += len(run_marker)

	next_step = text.find("\n      - name: ", run_start)
	if next_step == -1:
		next_step = len(text)

	block = text[run_start:next_step]
	return "\n".join(
		line[10:] if line.startswith("          ") else line
		for line in block.splitlines()
	)


def test_payload_first_fallback_and_shared_helper_usage() -> None:
	text = _workflow_text()

	assert "PR_TITLE: ${{ github.event.pull_request.title }}" in text
	assert "PR_BODY: ${{ github.event.pull_request.body || '' }}" in text
	assert 'PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"' in text
	assert 'extract_repo_scoped_issue_refs_from_text "${REPOSITORY}" "${PR_DATA}"' in text
	assert 'set_issue_phase_label_resilient "${issue_number}" "${FINAL_LABEL}" "${REPOSITORY}"' in text
	assert "_AI_PHASE_LABELS='[\"ai:done\"" not in text
	assert 'pulls/${PR_NUMBER}' not in text, (
		"Linked-issue fallback must use the pull_request event payload as its sole "
		"title/body source and must not refetch the PR."
	)


def test_issue_pr_status_bootstraps_revalidate_lifecycle_ai_memory_schemas() -> None:
	text = _workflow_text()
	assert "validation_history.v1.json" in text
	assert "operator_bypass_audit.v1.json" in text
	assert "revalidate_events.v1.json" in text


def test_lineage_finalization_noop_paths_emit_ai_memory_telemetry_before_exit() -> None:
	finalize_step = _step_script("Finalize linked issue lineage state")

	assert 'AI_MEMORY_ENABLED_NORMALIZED=false' in finalize_step
	assert '1|true|yes|on) AI_MEMORY_ENABLED_NORMALIZED=true ;;' in finalize_step
	assert 'if [ "${AI_MEMORY_ENABLED_NORMALIZED}" != "true" ]; then' in finalize_step

	no_link_message = 'echo "No linked issues found; skipping lineage finalization."'
	no_link_telemetry = (
		'echo "AI_MEMORY_TELEMETRY: {\\"op\\":\\"finalize-task\\",\\"ok\\":true,\\"enabled\\":${AI_MEMORY_ENABLED_NORMALIZED},\\"fail_open\\":true,\\"reason\\":\\"no_linked_issues\\",\\"source\\":\\"issue_pr_status.yml\\"}"'
	)
	disabled_message = 'echo "AI memory disabled; skipping lineage finalization."'
	disabled_telemetry = (
		'echo "AI_MEMORY_TELEMETRY: {\\"op\\":\\"finalize-task\\",\\"ok\\":true,\\"enabled\\":${AI_MEMORY_ENABLED_NORMALIZED},\\"fail_open\\":true,\\"reason\\":\\"ai_memory_disabled\\",\\"source\\":\\"issue_pr_status.yml\\"}"'
	)

	assert no_link_message in finalize_step
	assert no_link_telemetry in finalize_step
	assert disabled_message in finalize_step
	assert disabled_telemetry in finalize_step

	no_link_message_pos = finalize_step.find(no_link_message)
	no_link_telemetry_pos = finalize_step.find(no_link_telemetry)
	no_link_exit_pos = finalize_step.find("exit 0", no_link_message_pos)
	assert no_link_message_pos != -1
	assert no_link_telemetry_pos != -1
	assert no_link_exit_pos != -1
	assert no_link_message_pos < no_link_telemetry_pos < no_link_exit_pos

	disabled_message_pos = finalize_step.find(disabled_message)
	disabled_telemetry_pos = finalize_step.find(disabled_telemetry)
	disabled_exit_pos = finalize_step.find("exit 0", disabled_message_pos)
	assert disabled_message_pos != -1
	assert disabled_telemetry_pos != -1
	assert disabled_exit_pos != -1
	assert disabled_message_pos < disabled_telemetry_pos < disabled_exit_pos


def test_orchestrator_classification_is_exported_for_downstream_reuse() -> None:
	update_step = _step_script("Update linked issue labels when PR closes")

	assert 'ORCHESTRATOR_CLASSIFICATION_COMPLETE="true"' in update_step
	assert "export_orchestrator_issue_classification()" in update_step
	assert 'echo "TRACKING_ISSUES<<EOF" >> "$GITHUB_ENV"' in update_step
	assert 'echo "MANAGED_ISSUES<<EOF" >> "$GITHUB_ENV"' in update_step
	assert (
		'echo "ORCHESTRATOR_CLASSIFICATION_COMPLETE=${ORCHESTRATOR_CLASSIFICATION_COMPLETE}" >> "$GITHUB_ENV"'
	) in update_step
	assert 'ORCHESTRATOR_CLASSIFICATION_COMPLETE="false"' in update_step, (
		"REST fallback metadata failures must mark the reused classifier as incomplete "
		"so the merged-alert step can take its safe fallback path."
	)

	no_issue_pos = update_step.find('echo "No linked issues found for PR #${PR_NUMBER}."')
	export_before_exit_pos = update_step.find("export_orchestrator_issue_classification", no_issue_pos)
	exit_pos = update_step.find("exit 0", no_issue_pos)
	assert no_issue_pos != -1
	assert export_before_exit_pos != -1
	assert exit_pos != -1
	assert no_issue_pos < export_before_exit_pos < exit_pos, (
		"Even the no-linked-issues exit path must export empty classifier state so later "
		"steps can consume a defined env contract."
	)

	loop_end_pos = update_step.rfind('done <<< "${ISSUE_NUMBERS}"')
	final_export_pos = update_step.rfind("export_orchestrator_issue_classification")
	assert loop_end_pos != -1
	assert final_export_pos != -1
	assert loop_end_pos < final_export_pos, (
		"Normal success path must export the classifier result after issue processing completes."
	)


def test_fallback_regex_drops_bare_mentions_keeps_closing_keywords_and_urls() -> None:
	"""Regression guard for issue #1469.

	The previous fallback parser matched bare prose like ``issue #1469`` and
	bare paths like ``issues/1469``, so any PR body merely *mentioning* an
	issue would be treated as a closing link by this workflow. That caused
	orchestrator tracking issues to be wrongly labeled ai:merged and
	auto-closed when a sub-issue PR's body referenced them in passing.

	This test pins the tightened regex to GitHub closing-keyword semantics
	plus full repo-scoped URLs/paths, and forbids re-introducing the two
	bare-mention patterns.
	"""
	text = _workflow_text()

	assert 'extract_repo_scoped_issue_refs_from_text "${REPOSITORY}" "${PR_DATA}"' in text, (
		"Fallback linked-issue extraction must reuse the shared strict helper"
	)
	assert "REPOSITORY_ESCAPED=" not in text, (
		"Inline fallback regex copies should be removed once the shared helper is used"
	)

	# Forbidden: bare prose mentions that triggered the original bug.
	assert 'issue[[:space:]]*#[[:space:]]*[0-9]+' not in text, (
		"Bare 'issue #N' fallback pattern must not be re-introduced — it caused "
		"incorrect ai:merged labeling on issue #1469."
	)
	assert '(^|[^[:alnum:]_/-])issues/[0-9]+' not in text, (
		"Bare 'issues/N' fallback pattern must not be re-introduced — it caused "
		"incorrect ai:merged labeling on issues mentioned in PR prose."
	)
	assert '[[:space:]]*:?[[:space:]]*#[[:space:]]*[0-9]+' not in text, (
		"Optional colon between closing keyword and '#N' must not be re-introduced; "
		"GitHub closing-link syntax expects whitespace separation."
	)


def test_merged_alert_reuses_exported_managed_classification_before_body_lookup_fallback() -> None:
	alert_step = _step_script("Send PR merged Telegram alert")

	assert 'if [ -n "${MANAGED_ISSUES:-}" ]; then' in alert_step, (
		"Merged-alert step must consult exported MANAGED_ISSUES on the common path"
	)
	assert (
		'elif [ "${ORCHESTRATOR_CLASSIFICATION_COMPLETE:-false}" != "true" ] && [ -n "${LINKED_ISSUE_NUMBERS:-}" ]; then'
	) in alert_step, (
		"Body-lookup fallback must only run when the earlier classifier is incomplete"
	)
	assert "falling back to per-issue body lookup for PR merged alert suppression" in alert_step
	assert '_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq ' in alert_step, (
		"Per-issue issue lookup must remain available for the incomplete-classifier fallback path"
	)

	managed_pos = alert_step.find('if [ -n "${MANAGED_ISSUES:-}" ]; then')
	fallback_pos = alert_step.find(
		'elif [ "${ORCHESTRATOR_CLASSIFICATION_COMPLETE:-false}" != "true" ] && [ -n "${LINKED_ISSUE_NUMBERS:-}" ]; then'
	)
	loop_pos = alert_step.find("while IFS= read -r issue_number; do")
	lookup_pos = alert_step.find('_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq ')
	assert managed_pos != -1
	assert fallback_pos != -1
	assert loop_pos != -1
	assert lookup_pos != -1
	assert managed_pos < fallback_pos < loop_pos < lookup_pos, (
		"Merged-alert step must check exported classification first, and only then enter the "
		"legacy per-issue fallback lookup."
	)


def test_merged_alert_fallback_preserves_managed_label_or_body_detection() -> None:
	alert_step = _step_script("Send PR merged Telegram alert")

	assert 'index("ai:orchestrator-tracking")' in alert_step, (
		"Incomplete-classifier fallback must keep tracking precedence over managed detection"
	)
	assert 'index("ai:orchestrator-managed")' in alert_step, (
		"Incomplete-classifier fallback must keep the managed-label signal"
	)
	assert 'contains("Managed by: AI Orchestrator")' in alert_step, (
		"Incomplete-classifier fallback must keep the body-marker signal"
	)
	assert 'if [ "${ISSUE_IS_MANAGED}" = "true" ]; then' in alert_step, (
		"Fallback lookup must normalize its label-or-body check into a boolean gate"
	)

	lookup_pos = alert_step.find('_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq ')
	tracking_pos = alert_step.find('index("ai:orchestrator-tracking")')
	label_pos = alert_step.find('index("ai:orchestrator-managed")')
	body_pos = alert_step.find('contains("Managed by: AI Orchestrator")')
	match_pos = alert_step.find('if [ "${ISSUE_IS_MANAGED}" = "true" ]; then')
	assert lookup_pos != -1
	assert tracking_pos != -1
	assert label_pos != -1
	assert body_pos != -1
	assert match_pos != -1
	assert lookup_pos < tracking_pos < label_pos < body_pos < match_pos, (
		"Fallback lookup must preserve tracking precedence while evaluating the managed label and body marker before suppressing the alert."
	)


def test_orchestrator_tracking_issues_are_skipped_in_label_close_loop() -> None:
	"""Regression guard for issue #1469.

	When a PR's body mentions an orchestrator tracking issue (or one is
	resolved via the regex fallback), this workflow must not relabel or
	auto-close it. Terminal-phase ownership for orchestrator project
	tracking issues belongs exclusively to scripts/orchestrate_poll_process.sh.

	Tracking issues carry the `ai:orchestrator-tracking` label; the
	classifier must skip them based on that label (the parent body does
	NOT contain the "Managed by: AI Orchestrator" marker — that marker
	identifies child issues, see orchestrate_poll_process.sh:8406).
	"""
	text = _workflow_text()

	# The detection step builds an aliased GraphQL query that fetches both
	# labels and body for every linked issue in a single API call.
	assert "ORCH_ALIAS_FRAGMENT" in text, (
		"Expected batched GraphQL detection of orchestrator-related issues"
	)
	assert ' i${ORCH_IDX}: issue(number: ${_orch_num}) { number labels(first: 50) { nodes { name } } body }' in text, (
		"Expected aliased GraphQL fragment that fetches labels+body in one call"
	)

	# Tracking detection MUST be by label only — the parent tracking body
	# does not carry "Managed by: AI Orchestrator", so a body-based check
	# would mis-classify children as tracking and re-introduce the
	# stranded-child bug.
	assert 'index("ai:orchestrator-tracking")' in text

	# GraphQL validity requires complete aliased-issue coverage; any missing/null
	# alias entry must trigger REST fallback.
	assert 'and (([.data.repository | to_entries[] | .value | select(. != null)] | length) == $expected)' in text

	# Fail-open contract: GraphQL failure must fall back to per-issue REST,
	# not silently degrade detection.
	assert "Orchestrator-issue batch detection failed; falling back to per-issue REST" in text, (
		"GraphQL failure path must use per-issue REST fallback so a transient "
		"GraphQL error cannot re-introduce the auto-label bug."
	)
	assert 'gh_retry gh api "repos/${REPOSITORY}/issues/${_orch_num}" --jq' in text, (
		"Per-issue REST fallback lookup must remain wired in the GraphQL-failure path."
	)

	# Loop guard: the label/close loop must consult TRACKING_ISSUES
	# and continue (skip) before touching the label or issue state.
	assert (
		'if [ -n "${TRACKING_ISSUES}" ] && '
		'printf \'%s\\n\' "${TRACKING_ISSUES}" | grep -qxF "${issue_number}"; then'
	) in text, "Expected TRACKING_ISSUES skip gate inside the label/close loop"

	# Detection block must precede the label/close loop.
	detect_pos = text.find("TRACKING_ISSUES=\"\"")
	gate_pos = text.find('Skipping orchestrator-tracking issue #${issue_number}')
	label_call_pos = text.find('set_issue_phase_label_resilient "${issue_number}" "${FINAL_LABEL}" "${REPOSITORY}"')
	close_call_pos = text.find('gh_retry gh issue close "${issue_number}" -R "${REPOSITORY}"')
	assert detect_pos != -1, "Detection block missing"
	assert gate_pos != -1, "Tracking-skip log marker missing inside label/close loop"
	assert label_call_pos != -1, "set_issue_phase_label_resilient call missing"
	assert close_call_pos != -1, "gh issue close call missing"
	assert detect_pos < gate_pos < label_call_pos, (
		"Orchestrator detection must run before, and the skip gate must precede, "
		"the label apply call."
	)
	assert gate_pos < close_call_pos, (
		"Skip gate must precede the gh issue close call."
	)

	# Conservative fail-open contract: when the per-issue REST lookup also
	# fails, classify as tracking (skip) rather than managed (close). This
	# keeps the #1469 regression closed even when GitHub is degraded.
	assert "conservatively treating as tracking (skip)" in text, (
		"REST fallback must default to tracking-skip on metadata fetch failure"
	)


def test_orchestrator_managed_children_are_relabeled_and_closed_on_pr_merge() -> None:
	"""Regression guard for the orchestrator-child stranding recurrence.

	Orchestrator-managed child issues carry the `ai:orchestrator-managed`
	label and the "Managed by: AI Orchestrator" body marker. Their PRs
	always target an integration branch (`orchestrator/project-N`),
	never `main`, so the previous skip-everything-orchestrator rule
	combined with the `PR_BASE_REF == main` close gate left them stuck
	open on `ai:ready-to-merge` until the orchestrator poller eventually
	caught them via close_merged_issues_sweep.

	Policy:
	  - Detect children by `ai:orchestrator-managed` label OR the body
	    marker, but NOT if they also carry `ai:orchestrator-tracking`
	    (tracking takes precedence, skip wins).
	  - On PR close, set FINAL_LABEL on the child and close the issue
	    regardless of base ref.
	  - Non-orchestrator standalone issues continue to use the original
	    `PR_BASE_REF == main` close gate.
	"""
	text = _workflow_text()

	# Two distinct buckets must exist.
	assert "TRACKING_ISSUES=\"\"" in text, "Tracking bucket must be initialized"
	assert "MANAGED_ISSUES=\"\"" in text, "Managed-children bucket must be initialized"

	# Managed-child detection must include the label AND body marker as
	# either signal, but must EXCLUDE issues with the tracking label (so
	# tracking always wins).
	assert 'index("ai:orchestrator-managed")' in text, (
		"Managed-children classifier must check ai:orchestrator-managed label"
	)
	assert 'contains("Managed by: AI Orchestrator")' in text, (
		"Managed-children classifier must check the body marker"
	)
	assert 'index("ai:orchestrator-tracking")) == null' in text, (
		"Managed-children classifier must exclude issues that also carry "
		"ai:orchestrator-tracking (tracking takes precedence)"
	)

	# Loop must compute is_managed_child for the active issue.
	assert "is_managed_child=false" in text, "Default is_managed_child must be false"
	assert (
		'if [ -n "${MANAGED_ISSUES}" ] && '
		'printf \'%s\\n\' "${MANAGED_ISSUES}" | grep -qxF "${issue_number}"; then'
	) in text, "Loop must consult MANAGED_ISSUES for the current issue"
	assert "is_managed_child=true" in text, "Loop must flip is_managed_child when matched"

	# Close gate must include the managed-child branch — closing the
	# issue when its PR merges into orchestrator/project-N (base != default).
	assert (
		'if [ "${PR_MERGED}" != "true" ] || [ "${PR_BASE_REF}" = "${PR_DEFAULT_BRANCH}" ] || [ "${is_managed_child}" = "true" ]; then'
	) in text, (
		"Close gate must close on PR_MERGED!=true, PR_BASE_REF==default, "
		"OR is_managed_child==true"
	)
	assert "Closing orchestrator-managed child issue #${issue_number}" in text, (
		"Managed-child close path must emit a distinguishing log line"
	)

	# Managed-child detection must run before the loop touches the label.
	managed_classify_pos = text.find('index("ai:orchestrator-managed")')
	loop_check_pos = text.find('is_managed_child=true')
	label_call_pos = text.find('set_issue_phase_label_resilient "${issue_number}" "${FINAL_LABEL}" "${REPOSITORY}"')
	assert managed_classify_pos != -1
	assert loop_check_pos != -1
	assert label_call_pos != -1
	assert managed_classify_pos < loop_check_pos < label_call_pos


def _extract_refs(text: str) -> list[int]:
	result = subprocess.run(
		["bash", "-c", 'source scripts/gh_helpers.sh; extract_repo_scoped_issue_refs_from_text "owner/repo" "$TEXT_INPUT"'],
		cwd=REPO_ROOT,
		env={**os.environ, "TEXT_INPUT": text, "PYTHONDONTWRITEBYTECODE": "1"},
		capture_output=True,
		text=True,
		check=True,
	)
	return [int(line) for line in result.stdout.splitlines()]


def _run_status_steps(body: str, *, base: str, default: str, merged: bool, managed: bool = False, tracking: bool = False) -> tuple[str, str, str]:
	update = _step_script("Update linked issue labels when PR closes")
	update = update.replace(
		"source scripts/gh_helpers.sh 2>/dev/null || true",
		"source scripts/gh_helpers.sh 2>/dev/null || true\n"
		"gh_retry() {\n"
		"  if [ \"${1:-}\" = gh ] && [ \"${2:-}\" = api ]; then\n"
		"    if [[ \"$*\" == *closingIssuesReferences* ]]; then printf '%s' \"$MOCK_CLOSING\";\n"
		"    else printf '%s' \"$MOCK_ALIAS\"; fi\n"
		"  elif [ \"${2:-}\" = issue ] && [ \"${3:-}\" = close ]; then\n"
		"    printf 'CLOSE %s\\n' \"$4\"\n"
		"  else return 1; fi\n"
		"}",
	)
	update = update.replace(
		"source scripts/label_helpers.sh",
		"ensure_label_exists() { :; }\n"
		"set_issue_phase_label_resilient() { printf 'LABEL %s %s\\n' \"$1\" \"$2\"; }",
	)
	alias_payload = {"data": {"repository": {"i0": {
		"number": 42,
		"labels": {"nodes": [{"name": "ai:orchestrator-tracking" if tracking else "ai:orchestrator-managed"}]} if managed or tracking else {"nodes": []},
		"body": "",
	}}}}
	with tempfile.TemporaryDirectory() as directory:
		env_file = Path(directory) / "github_env"
		env = {
		**os.environ,
		"PYTHONDONTWRITEBYTECODE": "1",
		"GITHUB_ENV": str(env_file),
		"REPOSITORY": "owner/repo",
		"PR_NUMBER": "99",
		"PR_HEAD_REF": "feature/neutral",
		"PR_BASE_REF": base,
		"PR_DEFAULT_BRANCH": default,
		"PR_MERGED": str(merged).lower(),
		"FINAL_LABEL": "ai:merged" if merged else "ai:closed",
		"PR_TITLE": "Phase PR",
		"PR_BODY": body,
		"MOCK_CLOSING": json.dumps({"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": []}}}}}),
		"MOCK_ALIAS": json.dumps(alias_payload),
	}
		result = subprocess.run(["bash", "-c", update], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True)
		exports = env_file.read_text(encoding="utf-8")
		finalize_numbers = re.search(r"LINEAGE_FINALIZE_ISSUE_NUMBERS<<EOF\n(.*?)\nEOF", exports, re.S)
		assert finalize_numbers is not None
		finalize = _step_script("Finalize linked issue lineage state")
		finalize = finalize.replace("${{ github.event.pull_request.merged }}", str(merged).lower())
		finalize = finalize.replace("${{ github.server_url }}", "https://github.com")
		finalize = finalize.replace(
			"source scripts/memory_helpers.sh",
			'memory_ensure_branch() { :; }; memory_finalize_task() { printf "FINALIZE %s\\n" "$*"; }',
		)
		env.update({
			"LINEAGE_FINALIZE_ISSUE_NUMBERS": finalize_numbers.group(1),
			"LINKED_ISSUE_NUMBERS": "42" if "42" in exports else "",
			"AI_MEMORY_ENABLED": "true",
			"MEMORY_HELPERS_READY": "1",
			"PR_URL": "https://github.com/owner/repo/pull/99",
			"WORKFLOW_NAME": "status",
			"RUN_ID": "1", "RUN_ATTEMPT": "1", "ACTOR": "bot",
		})
		lineage = subprocess.run(["bash", "-c", finalize], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True)
		return result.stdout, lineage.stdout, exports


def test_status_completion_only_on_default_or_managed_merges() -> None:
	comment = "Refs #42 https://github.com/owner/repo/issues/42#issuecomment-5908534539"
	labels, lineage, exports = _run_status_steps(comment, base="claude/implement-plan-x", default="main", merged=True)
	assert "LABEL " not in labels and "CLOSE " not in labels
	assert "FINALIZE " not in lineage
	assert "LINEAGE_FINALIZE_ISSUE_NUMBERS<<EOF\n\nEOF" in exports

	labels, lineage, _ = _run_status_steps("Fixes #42", base="claude/implement-plan-x", default="main", merged=True)
	assert "LABEL " not in labels and "FINALIZE " not in lineage

	labels, lineage, _ = _run_status_steps("Fixes #42", base="orchestrator/project-1", default="main", merged=True, managed=True)
	assert "LABEL 42 ai:merged" in labels and "CLOSE 42" in labels
	assert "FINALIZE " in lineage and "--final-state merged" in lineage

	labels, lineage, _ = _run_status_steps("Fixes #42", base="trunk", default="trunk", merged=True)
	assert "LABEL 42 ai:merged" in labels and "CLOSE 42" in labels
	assert "FINALIZE " in lineage and "--final-state merged" in lineage

	labels, lineage, _ = _run_status_steps("Fixes #42", base="trunk", default="", merged=True)
	assert "LABEL " not in labels and "FINALIZE " not in lineage

	labels, lineage, _ = _run_status_steps("Fixes #42", base="feature", default="trunk", merged=False)
	assert "LABEL 42 ai:closed" in labels and "CLOSE 42" in labels
	assert "FINALIZE " in lineage and "--final-state closed" in lineage
	labels, lineage, _ = _run_status_steps("Fixes #42", base="trunk", default="trunk", merged=True, tracking=True)
	assert "LABEL " not in labels and "CLOSE " not in labels and "FINALIZE " not in lineage


def test_comment_urls_do_not_create_issue_links() -> None:
	assert _extract_refs("Refs #42 https://github.com/owner/repo/issues/42#issuecomment-9") == []
	assert _extract_refs("[one](https://github.com/owner/repo/issues/42?q=(a)#reply)") == []
	assert _extract_refs("owner/repo/issues/42/extra#reply") == []
	assert _extract_refs("https://evilgithub.com/owner/repo/issues/42") == []
	assert _extract_refs("https://example.com/?next=owner/repo/issues/42") == []
	assert _extract_refs("https://example.com/?next=https://github.com/owner/repo/issues/42") == []
	assert _extract_refs("ftp://example.com/?next=owner/repo/issues/42") == []
	assert _extract_refs("https://github.com/owner/repo/issues/42#see-owner/repo/issues/43") == []
	assert _extract_refs("https://github.com/owner/repo/issues/42?next=owner/repo/issues/43#reply") == []
	assert _extract_refs(r"[one](https://github.com/owner/repo/issues/42?q=a\)b#reply)") == []
	assert _extract_refs("[one](https://github.com/owner/repo/issues/42#reply),[two](https://github.com/owner/repo/issues/43)") == [43]
	assert _extract_refs("[one](https://github.com/owner/repo/issues/42?q=(a)),[two](https://github.com/owner/repo/issues/43)") == [42, 43]
	assert _extract_refs("https://github.com/owner/repo/issues/42 github.com/owner/repo/issues/43") == [42, 43]
	assert _extract_refs("Fixes #42 fixes #43 /owner/repo/issues/44 owner/repo/issues/45") == [42, 43, 44, 45]
	labels, lineage, exports = _run_status_steps("https://github.com/owner/repo/issues/42#reply", base="trunk", default="trunk", merged=True)
	assert "LABEL " not in labels and "FINALIZE " not in lineage
	assert "LINEAGE_FINALIZE_ISSUE_NUMBERS<<EOF\n\nEOF" in exports


if __name__ == "__main__":
	test_payload_first_fallback_and_shared_helper_usage()
	test_issue_pr_status_bootstraps_revalidate_lifecycle_ai_memory_schemas()
	test_lineage_finalization_noop_paths_emit_ai_memory_telemetry_before_exit()
	test_orchestrator_classification_is_exported_for_downstream_reuse()
	test_fallback_regex_drops_bare_mentions_keeps_closing_keywords_and_urls()
	test_merged_alert_reuses_exported_managed_classification_before_body_lookup_fallback()
	test_merged_alert_fallback_preserves_managed_label_or_body_detection()
	test_orchestrator_tracking_issues_are_skipped_in_label_close_loop()
	test_orchestrator_managed_children_are_relabeled_and_closed_on_pr_merge()
	test_status_completion_only_on_default_or_managed_merges()
	test_comment_urls_do_not_create_issue_links()
	print("PASS")
