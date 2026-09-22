#!/usr/bin/env python3
"""Contract tests: smoke-test Telegram silencing must reach every sender.

The test-and-mark-stable release gate wants exactly one Telegram message
per run (its `notify` job). Every pipeline workflow the smoke fixture
triggers detects the fixture and writes `ALERT_MSG_LEVEL=SILENT` to
`$GITHUB_ENV`. That export is defeated whenever a Telegram step declares
its own step-level `ALERT_MSG_LEVEL:` from `vars`, because a step's `env:`
block wins over job-level values (actions/runner ActionRunner.cs merges
the env context, then the step env). Every step-level declaration must
therefore read `env.ALERT_MSG_LEVEL` first so the smoke SILENT value
takes precedence over repo vars and per-step knobs.

Also pins the two workflows that had no smoke detection at all:
validate.yml (new `alert_msg_level` input, passed as SILENT by the gate)
and check_failure_triage.yml (SILENT for PRs labelled `e2e-smoke-test`).

Finally pins the *detection* half of the chain, which the original
plumbing fix left untouched. Silencing has three links: detect the
fixture, export `ALERT_MSG_LEVEL=SILENT`, and have every step read it.
Link 3 was pinned above while link 1 was not, so `clarify.yml` and
`plan.yml` kept matching only the literal `[E2E Smoke Test]` and stayed
green while two of the gate's four fixture title shapes went undetected
and alerted on every release run. The detector tests below derive the
fixture set from test-and-mark-stable.yml itself, so a newly added
fixture shape that no detector matches fails CI instead of paging an
operator.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Workflows that export ALERT_MSG_LEVEL=SILENT on smoke detection. Every
# `ALERT_MSG_LEVEL: ${{ ... }}` declaration in these files is step-level
# (none declares it at job or workflow scope), so each one must consult
# the env context first.
SMOKE_SILENCING_WORKFLOWS = (
	"clarify.yml",
	"plan.yml",
	"implement.yml",
	"review_autofix.yml",
	"orchestrate.yml",
	"orchestrate_poll.yml",
	"orchestrate_clarify_respond.yml",
)

# Exact step-level declarations expected per workflow, so a future edit
# cannot silently drop one (count and text are both pinned).
EXPECTED_STEP_DECLARATIONS = {
	"clarify.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 3,
	"plan.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 4,
	"implement.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 2,
	"review_autofix.yml": [
		"${{ env.ALERT_MSG_LEVEL || vars.CONFLICT_RESOLVED_ALERT_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}",
		"${{ env.ALERT_MSG_LEVEL || vars.PR_PROCESSED_ALERT_LEVEL || 'SILENT' }}",
		"${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}",
		"${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}",
	],
	"orchestrate.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 1,
	"orchestrate_poll.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 2,
	"orchestrate_clarify_respond.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 2,
}

STEP_DECL_RE = re.compile(r"^\s+ALERT_MSG_LEVEL:\s*(\$\{\{.*\}\})\s*$")

# Detector used wherever a workflow tests an ISSUE TITLE for the release
# gate's smoke fixture. Anchored: real AI issue titles never begin with
# `[E2E `, and anchoring keeps a production issue that merely quotes a
# fixture tag mid-title (e.g. "Fix [E2E Smoke Test] flake") out of the
# fixture path.
ISSUE_TITLE_DETECTOR = r"^\[E2E "

# Detector used in review_autofix.yml for PR title/body. Deliberately
# NOT anchored: a smoke PR is titled "AI implementation for issue #N"
# (implement.yml:4551) and carries the fixture tag inside the body, so
# the marker is mid-string on the only path that matches.
PR_TEXT_DETECTOR = r"\[E2E "

# Number of issue-title detector sites expected per workflow, so a future
# edit cannot drop one or silently re-narrow it.
ISSUE_TITLE_DETECTOR_SITES = {
	"clarify.yml": 1,
	"plan.yml": 1,
	# Precheck (before the duplicate-PR Telegram step) + main detect step.
	"implement.yml": 2,
	# Linked-issue lookup; the PR title/body pair uses PR_TEXT_DETECTOR.
	"review_autofix.yml": 1,
}

# Every `[E2E <variant>]` tag that appears anywhere in the release gate.
# Extracted rather than hardcoded so a new fixture shape is picked up
# automatically.
FIXTURE_TAG_RE = re.compile(r"\[E2E [^\]\n]*\]")


def _read(name: str, base: Path = WORKFLOWS) -> str:
	return (base / name).read_text(encoding="utf-8")


def _step_declarations(text: str) -> list[str]:
	return [m.group(1) for line in text.splitlines() if (m := STEP_DECL_RE.match(line))]


def test_smoke_silencing_workflows_export_silent_on_detection() -> None:
	for name in SMOKE_SILENCING_WORKFLOWS:
		wf = _read(name)
		assert 'echo "ALERT_MSG_LEVEL=SILENT" >> "$GITHUB_ENV"' in wf, name


def test_every_step_level_alert_level_reads_env_context_first() -> None:
	for name in SMOKE_SILENCING_WORKFLOWS:
		decls = _step_declarations(_read(name))
		assert decls, f"{name}: expected step-level ALERT_MSG_LEVEL declarations"
		for decl in decls:
			assert decl.startswith("${{ env.ALERT_MSG_LEVEL ||"), (
				f"{name}: step-level ALERT_MSG_LEVEL must read env.ALERT_MSG_LEVEL first "
				f"so the smoke SILENT export is not overridden: {decl}"
			)


def test_step_level_alert_level_declarations_are_pinned() -> None:
	for name, expected in EXPECTED_STEP_DECLARATIONS.items():
		assert _step_declarations(_read(name)) == expected, name


def test_implement_precheck_silences_before_duplicate_pr_notification() -> None:
	wf = _read("implement.yml")
	precheck = wf.index("ISSUE_TITLE_PRECHECK=\"$(printf '%s' \"${ISSUE_PAYLOAD}\" | jq -r '.title // \"\"')\"")
	duplicate_step = wf.index("- name: Telegram duplicate PR notification")
	main_detect = wf.index("- name: Detect smoke test and silence Telegram alerts")
	assert precheck < duplicate_step < main_detect
	precheck_block = wf[precheck:duplicate_step]
	assert f"grep -qiE '{ISSUE_TITLE_DETECTOR}'" in precheck_block
	assert 'echo "ALERT_MSG_LEVEL=SILENT" >> "$GITHUB_ENV"' in precheck_block


def test_validate_alert_msg_level_input_is_declared_and_consumed() -> None:
	validate = _read("validate.yml")
	assert "      alert_msg_level:\n" in validate
	assert "ALERT_MSG_LEVEL: ${{ inputs.alert_msg_level || vars.ALERT_MSG_LEVEL || 'DEBUG' }}" in validate
	assert "ALERT_MSG_LEVEL: ${{ vars.ALERT_MSG_LEVEL || 'DEBUG' }}" not in validate

	for wrapper in (
		_read("internal-validate.yml"),
		_read("ai-validate.yml", REPO_ROOT / "workflow-templates"),
	):
		assert "      alert_msg_level:\n" in wrapper
		assert "alert_msg_level: ${{ inputs.alert_msg_level || '' }}" in wrapper


def test_release_gate_dispatches_standalone_validate_silent() -> None:
	gate = _read("test-and-mark-stable.yml")
	start = gate.index("- name: Dispatch internal-validate.yml standalone")
	end = gate.index('- name: "Soft-error analyser (validate-standalone)"', start)
	block = gate[start:end]
	assert "--field tracking_issue=0" in block
	assert "--field alert_msg_level=SILENT" in block


def test_check_failure_triage_silences_smoke_prs() -> None:
	wf = _read("check_failure_triage.yml")
	assert "smoke_pr: ${{ steps.hash_check_name.outputs.smoke_pr }}" in wf
	assert (
		"if jq -e '[.labels[]?.name] | index(\"e2e-smoke-test\") != null' \"${pr_payload}\" >/dev/null 2>&1; then"
		in wf
	)
	assert 'echo "smoke_pr=true" >> "$GITHUB_OUTPUT"' in wf
	assert 'echo "smoke_pr=false" >> "$GITHUB_OUTPUT"' in wf
	assert (
		"ALERT_MSG_LEVEL: ${{ needs.derive_check_name_key.outputs.smoke_pr == 'true' && 'SILENT' || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"
		in wf
	)


def _gate_fixture_tags() -> set[str]:
	tags = set(FIXTURE_TAG_RE.findall(_read("test-and-mark-stable.yml")))
	assert tags, "expected the release gate to define [E2E ...] fixture tags"
	return tags


def test_issue_title_detector_matches_every_gate_fixture_tag() -> None:
	"""The anchored detector must match every fixture shape the gate builds.

	Regression guard for the release-run alert leak: the gate creates four
	title shapes and the old `\\[E2E Smoke Test\\]` literal matched only two.
	`[E2E Clarify Negative Test]` has a different middle token and
	`[E2E Smoke Test alt-model]` has no `]` directly after `Test`, so both
	sent per-phase Telegram alerts on every release run.
	"""
	detector = re.compile(ISSUE_TITLE_DETECTOR, re.IGNORECASE)
	for tag in sorted(_gate_fixture_tags()):
		title = f"{tag} some task description (run 123)"
		assert detector.search(title), (
			f"issue-title detector {ISSUE_TITLE_DETECTOR!r} does not match gate "
			f"fixture title {title!r}; a release run would alert on it"
		)


def test_issue_title_detector_ignores_production_titles() -> None:
	detector = re.compile(ISSUE_TITLE_DETECTOR, re.IGNORECASE)
	for title in (
		"Fix the release gate retry budget",
		"Update E2E smoke canary A for run 35672590166",
		"Fix [E2E Smoke Test] flake in the gate",
	):
		assert not detector.search(title), (
			f"issue-title detector must not treat {title!r} as the fixture"
		)


def test_issue_title_detector_sites_are_pinned() -> None:
	needle = f"grep -qiE '{ISSUE_TITLE_DETECTOR}'"
	for name, expected in ISSUE_TITLE_DETECTOR_SITES.items():
		found = _read(name).count(needle)
		assert found == expected, (
			f"{name}: expected {expected} issue-title detector site(s) using "
			f"{needle!r}, found {found}"
		)


def test_review_autofix_pr_text_detector_is_unanchored() -> None:
	"""PR title/body detection must stay mid-string, unlike issue titles."""
	wf = _read("review_autofix.yml")
	assert f"""grep -qiE '{PR_TEXT_DETECTOR}' \\\n""" in wf, (
		"review_autofix.yml PR-title check must use the unanchored detector"
	)
	assert f"""|| echo "${{PR_BODY}}" | grep -qiE '{PR_TEXT_DETECTOR}'; then""" in wf, (
		"review_autofix.yml PR-body check must use the unanchored detector"
	)


def test_plan_falls_back_to_orchestrator_parent_title() -> None:
	"""Orchestrator children carry no fixture marker, so plan must look up the parent.

	The gate's orchestrate-decompose-test job hands a
	`[E2E Orchestrate Smoke <run_id>]` project description to the
	decomposer, which writes the child titles itself (issues #4249/#4250
	of run 35672590166 were "Update E2E smoke canary A/B for run ..."). No
	title pattern can match those, so plan.yml resolves the parent
	tracking issue and reuses orchestrate_clarify_respond.yml's signal.
	"""
	wf = _read("plan.yml")
	start = wf.index("- name: Detect smoke test and tune LLM settings")
	end = wf.index("- name: Fetch issue comments", start)
	block = wf[start:end]

	# Parses the tracking ref out of the child body.
	assert "Tracking issue: #" in block
	# Reuses the parent-title signal orchestrate_clarify_respond.yml uses.
	assert "grep -qiE '^\\[Orchestrator\\] E2E '" in block
	assert "grep -qiE '^\\[Orchestrator\\] E2E '" in _read(
		"orchestrate_clarify_respond.yml"
	)
	# Needs a token for the parent lookup.
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in block
	# Fail-open: an unreachable parent must not silence a real project.
	assert "2>/dev/null" in block
	# Only reached when the title check missed, so the extra API call is
	# bounded to orchestrator-managed plan runs (CLAUDE.md §15).
	assert 'if [ "${IS_SMOKE_PLAN}" = "false" ]; then' in block
	assert 'echo "ALERT_MSG_LEVEL=SILENT" >> "$GITHUB_ENV"' in block


def _run() -> None:
	import inspect
	import sys

	tests = [
		obj
		for name, obj in sorted(globals().items())
		if name.startswith("test_") and inspect.isfunction(obj)
	]
	failures = 0
	for test in tests:
		try:
			test()
			print(f"PASS {test.__name__}")
		except AssertionError as exc:
			failures += 1
			print(f"FAIL {test.__name__}: {exc}")
	if failures:
		sys.exit(1)
	print(f"{len(tests)} tests passed")


if __name__ == "__main__":
	_run()
