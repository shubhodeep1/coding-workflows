"""Workflow contract for the plan autopilot job (issue #10091).

Text-based on purpose: the slices below pin the exact gate, job shape and
secret boundaries in .github/workflows/orchestrate_poll.yml.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLL = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
CLARIFY_FILES = (
	REPO_ROOT / ".github" / "workflows" / "clarify.yml",
	REPO_ROOT / ".github" / "workflows" / "internal-clarify.yml",
	REPO_ROOT / "workflow-templates" / "ai-clarify.yml",
)
LABELS = ("ai:plan-autopilot", "ai:plan-decision")


def _text() -> str:
	return POLL.read_text(encoding="utf-8")


def _job(name: str) -> str:
	text = _text()
	start = text.index(f"\n  {name}:\n")
	match = re.search(r"\n  [A-Za-z0-9_-]+:\n", text[start + 1:])
	end = start + 1 + match.start() if match else len(text)
	return text[start:end]


def _steps(job: str) -> list[str]:
	parts = re.split(r"\n      - name: ", job)
	return ["name: " + p for p in parts[1:]]


def _step(job: str, name: str) -> str:
	for step in _steps(job):
		if step.startswith(f"name: {name}\n"):
			return step
	raise AssertionError(f"step {name!r} missing")


def test_poll_outputs_and_idle_path_gate() -> None:
	poll = _job("poll")
	assert "plan_autopilot_due: ${{ steps.plan_autopilot_seen.outcome == 'success' && steps.plan_autopilot_seen.outputs.cache-hit != 'true' && 'true' || 'false' }}" in poll
	assert "plan_autopilot_key: ${{ steps.plan_autopilot_slot.outputs.key }}" in poll
	assert "plan_autopilot_support_ref: ${{ steps.plan_autopilot_slot.outputs.support_ref }}" in poll
	slot = _step(poll, "Check plan autopilot slot")
	assert "id: plan_autopilot_slot" in slot
	assert "if: vars.PLAN_AUTOPILOT_ENABLED == 'true' && steps.find_tracking.outcome == 'success' && steps.find_tracking.outputs.has_tracking == 'false'" in slot
	assert "gh " not in slot and "curl" not in slot
	assert "plan-autopilot-v1-${PLAN_AUTOPILOT_SHA}-$(date -u +%Y%m%d)" in slot
	seen = _step(poll, "Look up plan autopilot marker")
	assert "uses: actions/cache/restore@v4" in seen and "lookup-only: true" in seen
	assert "continue-on-error: true" in seen
	# Both steps come straight after find_tracking, whose idle early exit
	# only ends that step, so they run when no project is open.
	names = [s.split("\n", 1)[0] for s in _steps(poll)]
	idx = names.index("name: Find active tracking issues")
	assert names[idx + 1:idx + 3] == ["name: Check plan autopilot slot", "name: Look up plan autopilot marker"]


def test_enable_variable_defaults_off() -> None:
	text = _text()
	assert "vars.PLAN_AUTOPILOT_ENABLED == 'true'" in text
	assert "vars.PLAN_AUTOPILOT_ENABLED ||" not in text
	assert "PLAN_AUTOPILOT_MAINTENANCE_PR_STALE_HOURS: ${{ vars.PLAN_AUTOPILOT_MAINTENANCE_PR_STALE_HOURS || '24' }}" in text
	assert "PLAN_AUTOPILOT_CODEX_MODEL: ${{ vars.PLAN_AUTOPILOT_CODEX_MODEL || '' }}" in _job("plan_autopilot")
	switches = (REPO_ROOT / "scripts" / "stage_switches.json").read_text(encoding="utf-8")
	assert "PLAN_AUTOPILOT_ENABLED" not in switches


def test_job_shape() -> None:
	job = _job("plan_autopilot")
	assert "\n    needs: poll\n" in job
	assert "if: ${{ !cancelled() && needs.poll.outputs.plan_autopilot_due == 'true' }}" in job
	assert "timeout-minutes: 90" in job
	assert "group: plan-autopilot-${{ github.repository }}" in job
	assert "cancel-in-progress: false" in job
	assert "runs-on: ${{ vars.PIPELINE_RUNS_ON == 'do-runner' && 'do-runner' || 'ubuntu-latest' }}" in job
	text = _text()
	comment = text[:text.index("\n  plan_autopilot:\n")][-1500:]
	assert "§26" in comment and "Jobs per poll tick" in comment


def test_script_steps_are_one_line_calls() -> None:
	job = _job("plan_autopilot")
	subcommands = []
	for step in _steps(job):
		if "scripts/plan_autopilot.py " not in step or "run: |" in step:
			continue
		match = re.search(r"\n        run: (python3 -I -B \.codex-workflow-src/scripts/plan_autopilot\.py (\w+)[^\n]*)\n?", step + "\n")
		assert match, step
		assert "${{" not in match.group(1)
		subcommands.append(match.group(2))
	assert subcommands == ["gate", "collect", "engine", "audit", "apply", "decisions", "dispatch", "report"]


def test_audit_step_gets_no_github_token() -> None:
	job = _job("plan_autopilot")
	audit = _step(job, "Run plan audit")
	assert "GH_TOKEN" not in audit and "GH_PAT" not in audit and "GITHUB_TOKEN" not in audit
	assert "AI_ENGINE_PLAN_AUDIT: ${{ steps.engine.outputs.engine || 'codex' }}" in audit
	for name in ("Plan autopilot gate", "Collect plan audit input", "Apply plan audit changes", "Start the recommended plan", "Report plan autopilot status"):
		assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in _step(job, name)
	checkout = _step(job, "Checkout repository for the plan audit")
	assert "persist-credentials: false" in checkout


def test_marker_saved_only_from_report_decision() -> None:
	job = _job("plan_autopilot")
	recheck = _step(job, "Re-check plan autopilot marker")
	assert "key: ${{ needs.poll.outputs.plan_autopilot_key }}" in recheck and "lookup-only: true" in recheck
	gate = _step(job, "Plan autopilot gate")
	assert "steps.plan_autopilot_recheck.outputs.cache-hit != 'true'" in gate
	save = _step(job, "Save plan autopilot marker")
	assert "if: always() && steps.report.outputs.save_marker == 'true'" in save
	assert "uses: actions/cache/save@v4" in save
	assert "key: ${{ needs.poll.outputs.plan_autopilot_key }}" in save
	report = _step(job, "Report plan autopilot status")
	assert "if: always() && steps.gate.outcome == 'success'" in report


def test_support_checkout_is_pinned_to_poll_ref() -> None:
	job = _job("plan_autopilot")
	support = _step(job, "Checkout plan autopilot support")
	assert "ref: ${{ needs.poll.outputs.plan_autopilot_support_ref }}" in support
	verify = _step(job, "Verify plan autopilot support commit")
	assert "refusing to run" in verify


def test_clarify_predicates_skip_both_labels() -> None:
	for path in CLARIFY_FILES:
		text = path.read_text(encoding="utf-8")
		for label in LABELS:
			assert f"!contains(toJson(github.event.issue.labels.*.name), '{label}')" in text, (path, label)


def test_label_contract_and_helper_entries() -> None:
	contract = json.loads((REPO_ROOT / ".github" / "ai" / "label_contract.v1.json").read_text(encoding="utf-8"))
	helpers = (REPO_ROOT / "scripts" / "label_helpers.sh").read_text(encoding="utf-8")
	expected = {
		"ai:plan-autopilot": ("5319e7", "Stable status issue for the plan autopilot"),
		"ai:plan-decision": ("fbca04", "Plan the autopilot cannot refresh without a decision"),
	}
	for label, (color, description) in expected.items():
		assert contract["labels"][label] == {"color": color, "description": description}
		assert f'["{label}"]="{color}"' in helpers
		assert f'["{label}"]="{description}"' in helpers


def test_workflow_stays_under_size_guard() -> None:
	assert POLL.stat().st_size < 480_000
