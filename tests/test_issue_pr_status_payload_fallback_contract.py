#!/usr/bin/env python3
"""Contract tests for payload-first fallback in issue_pr_status workflow."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"


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
	# issue when its PR merges into orchestrator/project-N (base != main).
	assert (
		'if [ "${PR_MERGED}" != "true" ] || [ "${PR_BASE_REF}" = "${REPO_DEFAULT_BRANCH}" ] || [ "${managed_integration_merge}" = "true" ]; then'
	) in text, (
		"Completion gate must hold on PR_MERGED!=true, PR_BASE_REF==default branch, "
		"OR a managed child merged into its integration branch"
	)
	assert 'if [ "${is_managed_child}" = "true" ]; then' in text
	assert '[[ "${PR_BASE_REF}" == orchestrator/project-* ]]' in text
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


# ---------------------------------------------------------------------------
# Behavioural harness (#5776 / #6632): run the real "Update linked issue labels
# when PR closes" and "Finalize linked issue lineage state" step scripts with a
# fake `gh`, stub label/memory helpers and the real gh_helpers.sh.
# ---------------------------------------------------------------------------

_FAKE_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
args="$*"
if [ "$1" = "issue" ] && [ "$2" = "close" ]; then
  printf 'close %s\n' "$3" >> "${CALLS_LOG}"
  exit 0
fi
if [ "$1" = "api" ] && [ "$2" = "graphql" ]; then
  case "${args}" in
    *closingIssuesReferences*) printf '%s' "${FAKE_CLOSING_JSON}"; exit 0 ;;
    *"i0: issue("*) printf '%s' "${FAKE_CLASSIFY_JSON}"; exit 0 ;;
  esac
fi
printf '{}'
"""

_LABEL_HELPERS_STUB = """ensure_label_exists() { printf 'ensure %s\\n' "$1" >> "${CALLS_LOG}"; }
set_issue_phase_label_resilient() { printf 'label %s %s\\n' "$1" "$2" >> "${CALLS_LOG}"; }
"""

_MEMORY_HELPERS_STUB = """memory_ensure_branch() { printf 'ensure_branch\\n' >> "${CALLS_LOG}"; }
memory_finalize_task() { printf 'finalize %s\\n' "$*" >> "${CALLS_LOG}"; }
"""


def _closing_json(nodes: list[dict]) -> str:
	return json.dumps(
		{"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": nodes}}}}}
	)


def _issue_node(number: int, labels: list[str] | None = None, body: str = "") -> dict:
	return {"number": number, "body": body, "labels": {"nodes": [{"name": n} for n in (labels or [])]}}


def _parse_github_env(path: Path) -> dict[str, str]:
	result: dict[str, str] = {}
	lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
	i = 0
	while i < len(lines):
		line = lines[i]
		if "<<" in line:
			key, delim = line.split("<<", 1)
			i += 1
			buf: list[str] = []
			while i < len(lines) and lines[i] != delim:
				buf.append(lines[i])
				i += 1
			result[key] = "\n".join(buf)
		elif "=" in line:
			key, value = line.split("=", 1)
			result[key] = value
		i += 1
	return result


def _run_status_sync(
	*,
	merged: bool,
	base_ref: str,
	default_branch: str,
	head_ref: str = "feature/x",
	body: str = "",
	title: str = "Change",
	closing_nodes: list[dict] | None = None,
	classify_nodes: list[dict] | None = None,
) -> dict:
	tmp = Path(tempfile.mkdtemp(prefix="issue-pr-status-"))
	try:
		(tmp / "scripts").mkdir()
		(tmp / "bin").mkdir()
		shutil.copy(GH_HELPERS, tmp / "scripts" / "gh_helpers.sh")
		(tmp / "scripts" / "label_helpers.sh").write_text(_LABEL_HELPERS_STUB, encoding="utf-8")
		(tmp / "scripts" / "memory_helpers.sh").write_text(_MEMORY_HELPERS_STUB, encoding="utf-8")
		fake_gh = tmp / "bin" / "gh"
		fake_gh.write_text(_FAKE_GH, encoding="utf-8")
		fake_gh.chmod(0o755)
		calls_log = tmp / "calls.log"
		calls_log.touch()
		github_env = tmp / "github_env"
		github_env.touch()

		classify = {
			"data": {
				"repository": {f"i{idx}": node for idx, node in enumerate(classify_nodes or [])}
			}
		}
		env = {
			"PATH": f"{tmp / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
			"HOME": str(tmp),
			"TMPDIR": str(tmp),
			"GITHUB_ENV": str(github_env),
			"CALLS_LOG": str(calls_log),
			"FAKE_GH_LOG": str(tmp / "gh.log"),
			"FAKE_CLOSING_JSON": _closing_json(closing_nodes or []),
			"FAKE_CLASSIFY_JSON": json.dumps(classify),
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"GH_TOKEN": "dummy",
			"REPOSITORY": "o/r",
			"PR_NUMBER": "5649",
			"PR_HEAD_REF": head_ref,
			"PR_BASE_REF": base_ref,
			"PR_DEFAULT_BRANCH": default_branch,
			"PR_MERGED": "true" if merged else "false",
			"PR_TITLE": title,
			"PR_BODY": body,
			"FINAL_LABEL": "ai:merged" if merged else "ai:closed",
		}
		update = subprocess.run(
			["bash", "-c", _step_script("Update linked issue labels when PR closes")],
			cwd=tmp, env=env, capture_output=True, text=True, timeout=120,
		)
		assert update.returncode == 0, update.stdout + update.stderr

		exported = _parse_github_env(github_env)
		lineage_env = dict(env)
		lineage_env.update(exported)
		lineage_env.update(
			{
				"AI_MEMORY_ENABLED": "true",
				"MEMORY_HELPERS_READY": "1",
				"PR_URL": "https://github.com/o/r/pull/5649",
				"WORKFLOW_NAME": "AI Issue PR Status Sync",
				"RUN_ID": "1",
				"RUN_ATTEMPT": "1",
				"ACTOR": "tester",
			}
		)
		lineage_script = (
			_step_script("Finalize linked issue lineage state")
			.replace("${{ github.event.pull_request.merged }}", "true" if merged else "false")
			.replace("${{ github.server_url }}", "https://github.com")
		)
		lineage = subprocess.run(
			["bash", "-c", lineage_script],
			cwd=tmp, env=lineage_env, capture_output=True, text=True, timeout=120,
		)
		assert lineage.returncode == 0, lineage.stdout + lineage.stderr
		return {
			"calls": calls_log.read_text(encoding="utf-8").splitlines(),
			"env": exported,
			"update_out": update.stdout,
			"lineage_out": lineage.stdout,
		}
	finally:
		shutil.rmtree(tmp, ignore_errors=True)


def _jq_missing(test_name: str) -> bool:
	"""Return True (and skip) when jq is unavailable; the step scripts need it."""
	if shutil.which("jq") is not None:
		return False
	message = f"{test_name}: jq not installed; behavioural status-sync test skipped"
	if "pytest" in sys.modules:
		import pytest

		pytest.skip(message)
	print(f"SKIP {message}")
	return True


def _label_calls(result: dict) -> list[str]:
	return [c for c in result["calls"] if c.startswith("label ")]


def _close_calls(result: dict) -> list[str]:
	return [c for c in result["calls"] if c.startswith("close ")]


def _finalize_calls(result: dict) -> list[str]:
	return [c for c in result["calls"] if c.startswith("finalize ")]


def test_non_default_merge_with_refs_and_comment_url_leaves_issue_untouched() -> None:
	"""Incident shape of PR #5649 (#5776): Refs #N plus a comment URL, merged into a project branch."""
	if _jq_missing("test_non_default_merge_with_refs_and_comment_url_leaves_issue_untouched"):
		return
	result = _run_status_sync(
		merged=True,
		base_ref="claude/implement-plan-x",
		default_branch="main",
		head_ref="claude/implement-plan-x",
		body=(
			"Refs #4867\n"
			"See https://github.com/o/r/issues/4867#issuecomment-5908534539 for context."
		),
	)
	assert _label_calls(result) == [], result
	assert _close_calls(result) == [], result
	assert _finalize_calls(result) == [], result
	assert '"reason":"no_linked_issues"' in result["lineage_out"], result["lineage_out"]


def test_non_default_merge_with_closing_ref_does_not_label_or_finalize() -> None:
	if _jq_missing("test_non_default_merge_with_closing_ref_does_not_label_or_finalize"):
		return
	result = _run_status_sync(
		merged=True,
		base_ref="claude/implement-plan-x",
		default_branch="main",
		closing_nodes=[_issue_node(12)],
		classify_nodes=[_issue_node(12)],
	)
	assert _label_calls(result) == [], result
	assert _close_calls(result) == [], result
	assert _finalize_calls(result) == [], result
	assert result["env"].get("LINEAGE_FINALIZE_ISSUE_NUMBERS", "") == ""
	assert result["env"].get("LINKED_ISSUE_NUMBERS", "").strip() == "12"
	assert "issue #12 labels, state and lineage left unchanged" in result["update_out"]
	assert '"reason":"non_completion_merge"' in result["lineage_out"], result["lineage_out"]


def test_managed_child_integration_merge_still_labels_closes_and_finalizes() -> None:
	if _jq_missing("test_managed_child_integration_merge_still_labels_closes_and_finalizes"):
		return
	managed = _issue_node(34, labels=["ai:orchestrator-managed"])
	result = _run_status_sync(
		merged=True,
		base_ref="orchestrator/project-9",
		default_branch="main",
		head_ref="ai/issue-34",
		closing_nodes=[managed],
		classify_nodes=[managed],
	)
	assert _label_calls(result) == ["label 34 ai:merged"], result
	assert _close_calls(result) == ["close 34"], result
	finalize = _finalize_calls(result)
	assert len(finalize) == 1 and "--issue-number 34" in finalize[0], finalize
	assert "--final-state merged" in finalize[0], finalize


def test_managed_child_non_integration_merge_leaves_issue_untouched() -> None:
	"""A managed child merged into a branch that is not its integration branch is no completion."""
	if _jq_missing("test_managed_child_non_integration_merge_leaves_issue_untouched"):
		return
	managed = _issue_node(
		35,
		labels=["ai:orchestrator-managed"],
		body="- Integration branch: `orchestrator/project-9`",
	)
	result = _run_status_sync(
		merged=True,
		base_ref="claude/implement-plan-x",
		default_branch="main",
		head_ref="ai/issue-35",
		closing_nodes=[managed],
		classify_nodes=[managed],
	)
	assert _label_calls(result) == [], result
	assert _close_calls(result) == [], result
	assert _finalize_calls(result) == [], result
	assert '"reason":"non_completion_merge"' in result["lineage_out"], result["lineage_out"]


def test_managed_child_other_project_branch_merge_leaves_issue_untouched() -> None:
	"""A declared integration branch wins over the orchestrator/project-* prefix rule."""
	if _jq_missing("test_managed_child_other_project_branch_merge_leaves_issue_untouched"):
		return
	managed = _issue_node(
		37,
		labels=["ai:orchestrator-managed"],
		body="- Integration branch: `orchestrator/project-9`",
	)
	result = _run_status_sync(
		merged=True,
		base_ref="orchestrator/project-10",
		default_branch="main",
		head_ref="ai/issue-37",
		closing_nodes=[managed],
		classify_nodes=[managed],
	)
	assert _label_calls(result) == [], result
	assert _close_calls(result) == [], result
	assert _finalize_calls(result) == [], result
	assert '"reason":"non_completion_merge"' in result["lineage_out"], result["lineage_out"]


def test_managed_child_declared_custom_integration_merge_completes() -> None:
	"""A managed child merged into the custom integration branch its body declares completes."""
	if _jq_missing("test_managed_child_declared_custom_integration_merge_completes"):
		return
	managed = _issue_node(
		36,
		labels=["ai:orchestrator-managed"],
		body="**Orchestrator metadata**\n- Integration branch: `release/integration-2`\n",
	)
	result = _run_status_sync(
		merged=True,
		base_ref="release/integration-2",
		default_branch="main",
		head_ref="ai/issue-36",
		closing_nodes=[managed],
		classify_nodes=[managed],
	)
	assert _label_calls(result) == ["label 36 ai:merged"], result
	assert _close_calls(result) == ["close 36"], result
	assert len(_finalize_calls(result)) == 1, result


def test_default_branch_merge_with_fixes_labels_closes_and_finalizes() -> None:
	if _jq_missing("test_default_branch_merge_with_fixes_labels_closes_and_finalizes"):
		return
	for default in ("trunk", "main"):
		result = _run_status_sync(
			merged=True,
			base_ref=default,
			default_branch=default,
			body="Fixes #56",
			classify_nodes=[_issue_node(56)],
		)
		assert _label_calls(result) == ["label 56 ai:merged"], (default, result)
		assert _close_calls(result) == ["close 56"], (default, result)
		finalize = _finalize_calls(result)
		assert len(finalize) == 1 and "--issue-number 56" in finalize[0], (default, finalize)
		assert "--final-state merged" in finalize[0], (default, finalize)


def test_unmerged_close_keeps_closed_label_close_and_lineage() -> None:
	if _jq_missing("test_unmerged_close_keeps_closed_label_close_and_lineage"):
		return
	result = _run_status_sync(
		merged=False,
		base_ref="feature-x",
		default_branch="main",
		closing_nodes=[_issue_node(78)],
		classify_nodes=[_issue_node(78)],
	)
	assert _label_calls(result) == ["label 78 ai:closed"], result
	assert _close_calls(result) == ["close 78"], result
	finalize = _finalize_calls(result)
	assert len(finalize) == 1 and "--final-state closed" in finalize[0], finalize


def test_tracking_issue_never_labelled_closed_or_lineage_finalized() -> None:
	"""Tracking issues stay poller-owned on every completion path, lineage included."""
	if _jq_missing("test_tracking_issue_never_labelled_closed_or_lineage_finalized"):
		return
	tracking = _issue_node(90, labels=["ai:orchestrator-tracking"])
	for merged in (True, False):
		result = _run_status_sync(
			merged=merged,
			base_ref="main",
			default_branch="main",
			closing_nodes=[tracking],
			classify_nodes=[tracking],
		)
		assert _label_calls(result) == [], (merged, result)
		assert _close_calls(result) == [], (merged, result)
		assert _finalize_calls(result) == [], (merged, result)
		assert result["env"].get("LINEAGE_FINALIZE_ISSUE_NUMBERS", "") == "", (merged, result)
		assert '"reason":"non_completion_merge"' in result["lineage_out"], (merged, result["lineage_out"])


def _extract_refs(text: str) -> str:
	proc = subprocess.run(
		[
			"bash",
			"-c",
			'source "$1" >/dev/null 2>&1; extract_repo_scoped_issue_refs_from_text o/r "$2"',
			"_",
			str(GH_HELPERS),
			text,
		],
		capture_output=True,
		text=True,
		timeout=60,
	)
	assert proc.returncode == 0, proc.stderr
	return proc.stdout.strip()


def test_extract_helper_ignores_fragment_urls_but_keeps_issue_links() -> None:
	cases = {
		"https://github.com/o/r/issues/4867#issuecomment-5908534539": "",
		"o/r/issues/9#discussion_r1": "",
		"https://github.com/o/r/issues/9?view=plain#issuecomment-1": "",
		"[x](https://github.com/o/r/issues/9#issuecomment-1)": "",
		"https://github.com/o/r/issues/9": "9",
		"o/r/issues/9.": "9",
		"Fixes #9": "9",
		"Refs #9": "",
	}
	for text, expected in cases.items():
		assert _extract_refs(text) == expected, (text, expected)


def test_completion_gate_uses_event_default_branch_without_run_interpolation() -> None:
	text = _workflow_text()
	update_step = _step_script("Update linked issue labels when PR closes")
	lineage_step = _step_script("Finalize linked issue lineage state")

	assert "PR_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}" in text
	assert '"${PR_BASE_REF}" = "main"' not in text
	assert "${{" not in update_step and "github.event.repository.default_branch" not in update_step, (
		"The default branch must reach the script only through env:"
	)
	assert 'REPO_DEFAULT_BRANCH="${PR_DEFAULT_BRANCH:-}"' in update_step
	assert 'done <<< "${LINEAGE_FINALIZE_ISSUE_NUMBERS}"' in lineage_step
	non_completion_pos = lineage_step.find("non_completion_merge")
	helpers_ready_pos = lineage_step.find('MEMORY_HELPERS_READY:-0')
	assert non_completion_pos != -1 and helpers_ready_pos != -1
	assert non_completion_pos < helpers_ready_pos


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
	test_non_default_merge_with_refs_and_comment_url_leaves_issue_untouched()
	test_non_default_merge_with_closing_ref_does_not_label_or_finalize()
	test_managed_child_integration_merge_still_labels_closes_and_finalizes()
	test_managed_child_non_integration_merge_leaves_issue_untouched()
	test_managed_child_other_project_branch_merge_leaves_issue_untouched()
	test_managed_child_declared_custom_integration_merge_completes()
	test_default_branch_merge_with_fixes_labels_closes_and_finalizes()
	test_unmerged_close_keeps_closed_label_close_and_lineage()
	test_tracking_issue_never_labelled_closed_or_lineage_finalized()
	test_extract_helper_ignores_fragment_urls_but_keeps_issue_links()
	test_completion_gate_uses_event_default_branch_without_run_interpolation()
	print("PASS")
