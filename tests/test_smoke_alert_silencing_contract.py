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
	assert "grep -qiE '\\[E2E Smoke Test\\b'" in precheck_block
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
