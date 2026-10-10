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
import subprocess
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
	# 4 = the three clarify notification steps + "Standalone auto-decide"
	# (replace-claude-sessions Phase 8b), whose answer poster's loop guard can
	# alert. The Claude issue handoff step was retired in Phase 2.
	"clarify.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 4,
	"plan.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 4,
	"implement.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 2,
	# Issue #6633 adds "Record Claude pool capacity", "Mark outage-caused
	# review-blocked labels (autofix exhaustion)" and "Record model-provider
	# outage" (first, third and seventh entries).
	"review_autofix.yml": [
		"${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}",
		"${{ env.ALERT_MSG_LEVEL || vars.CONFLICT_RESOLVED_ALERT_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}",
		"${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}",
		"${{ env.ALERT_MSG_LEVEL || vars.PR_PROCESSED_ALERT_LEVEL || 'SILENT' }}",
		"${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}",
		"${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}",
		"${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}",
	],
	"orchestrate.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 1,
	# 5 = includes "Alert on Claude pool accounts at the usage gate" (#6951)
	# and the unrouted-reply check in "Replay failed trusted reclarify commands" (#6630).
	"orchestrate_poll.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 5,
	# 3 = "Standalone RECOMMENDED fallback" (pages when the standalone worker
	# fails with no fallback), "Parse and post answer", the failure alert.
	"orchestrate_clarify_respond.yml": ["${{ env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'DEBUG' }}"] * 3,
}

STEP_DECL_RE = re.compile(r"^\s+ALERT_MSG_LEVEL:\s*(\$\{\{.*\}\})\s*$")

# Detector used wherever a workflow tests an ISSUE TITLE for the release
# gate's smoke fixture. Anchored: real AI issue titles never begin with
# `[E2E `, and anchoring keeps a production issue that merely quotes a
# fixture tag mid-title (e.g. "Fix [E2E Smoke Test] flake") out of the
# fixture path.
ISSUE_TITLE_DETECTOR = r"^\[E2E "

# review_autofix.yml's primary smoke signal. Free-text fixture-tag
# matching on the PR title/body was removed: it misclassified any real PR
# that merely discussed the tags, which pinned reasoning, silenced that
# PR's review alerts, and exported IS_SMOKE_TEST -- making
# review_apply_fixes.sh tell the editor it "must call apply_patch on
# tests/e2e_smoke_canary.txt" on an unrelated PR.
SMOKE_LABEL_JQ = (
	"""jq -e '[.labels[]?.name] | index("e2e-smoke-test") != null' """
	'"${PR_PAYLOAD_FILE}"'
)

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


def test_review_autofix_uses_label_as_primary_smoke_signal() -> None:
	"""The `e2e-smoke-test` label, not PR prose, decides smoke classification.

	implement.yml applies the label atomically at `gh pr create`, so it is
	already in the `pull_request: opened` payload this gate snapshots.
	Confirmed on release run 35672590166: PR #4252 (from the plain canary
	fixture) and PR #4253 (from the alt-model fixture) both carried it.
	"""
	wf = _read("review_autofix.yml")
	start = wf.index("- name: Detect smoke test and tune LLM settings")
	end = wf.index("if [ \"$IS_SMOKE\" = \"true\" ]; then", start)
	block = wf[start:end]

	assert SMOKE_LABEL_JQ in block, (
		"review_autofix.yml must gate IS_SMOKE on the e2e-smoke-test label"
	)
	# The anchored linked-issue title remains as the second signal.
	assert f"grep -qiE '{ISSUE_TITLE_DETECTOR}'" in block


def test_review_autofix_label_filter_handles_missing_labels() -> None:
	"""The exact workflow filter must treat an absent labels key as non-smoke."""
	jq_filter_match = re.search(r"jq -e '([^']+)'", SMOKE_LABEL_JQ)
	assert jq_filter_match is not None
	jq_filter_text = jq_filter_match.group(1)

	missing_labels_result = subprocess.run(
		["jq", "-e", jq_filter_text],
		input='{"title":"No pull request payload"}',
		capture_output=True,
		text=True,
		check=False,
	)
	assert missing_labels_result.returncode == 1, missing_labels_result.stderr
	assert missing_labels_result.stdout.strip() == "false"
	assert missing_labels_result.stderr == ""

	smoke_labels_result = subprocess.run(
		["jq", "-e", jq_filter_text],
		input='{"labels":[{"name":"bug"},{"name":"e2e-smoke-test"}]}',
		capture_output=True,
		text=True,
		check=False,
	)
	assert smoke_labels_result.returncode == 0, smoke_labels_result.stderr
	assert smoke_labels_result.stdout.strip() == "true"


def test_review_autofix_does_not_free_text_match_pr_title_or_body() -> None:
	"""Regression guard for the misclassification described in SMOKE_LABEL_JQ."""
	wf = _read("review_autofix.yml")
	for forbidden in (
		'echo "${PR_TITLE}" | grep -qiE \'\\[E2E ',
		'echo "${PR_BODY}" | grep -qiE \'\\[E2E ',
		"echo \"${PR_TITLE}\" | grep -qi '\\[E2E Smoke Test\\]'",
		"echo \"${PR_BODY}\" | grep -qi '\\[E2E Smoke Test\\]'",
	):
		assert forbidden not in wf, (
			"review_autofix.yml must not classify a PR as a smoke fixture from "
			f"its own title/body prose: {forbidden!r}"
		)


def test_implement_resolves_orchestrator_parent_for_label_reliability() -> None:
	"""implement.yml must detect decomposed children, or the label never lands.

	IS_SMOKE_TEST gates the atomic `force-review` + `e2e-smoke-test` labels
	at PR creation, and review_autofix.yml now treats that label as its
	primary signal. An orchestrator-decomposed child carries no fixture
	marker in its own title, so without the parent lookup its PR would be
	unlabelled and the whole downstream chain would misclassify it.
	"""
	wf = _read("implement.yml")
	precheck_start = wf.index("- name: Precheck approval phase label")
	precheck_end = wf.index("- name: Telegram duplicate PR notification")
	precheck = wf[precheck_start:precheck_end]

	# Parent lookup lives in the precheck, resolved once.
	assert "Tracking issue: #" in precheck
	assert "grep -qiE '^\\[Orchestrator\\] E2E '" in precheck
	assert 'echo "IS_SMOKE_PARENT=${IS_SMOKE_PARENT}" >> "$GITHUB_ENV"' in precheck
	# Fail-open on an unreachable parent.
	assert "2>/dev/null" in precheck

	# The main detect step reuses it rather than repeating the lookup, so
	# the run issues at most one extra API call (CLAUDE.md §15).
	main_start = wf.index("- name: Detect smoke test and silence Telegram alerts")
	main_end = wf.index("- name: Fetch issue comments", main_start)
	main = wf[main_start:main_end]
	assert '[ "${IS_SMOKE_PARENT:-false}" = "true" ]' in main
	assert "Tracking issue: #" not in main, (
		"main detect step must reuse IS_SMOKE_PARENT, not repeat the lookup"
	)
	assert 'echo "IS_SMOKE_TEST=true" >> "$GITHUB_ENV"' in main


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


# Poller silencing (operator decision Q42: A). The scheduled orchestrator
# poller does not inherit the release workflows' ALERT_MSG_LEVEL=SILENT, so a
# poll that processed a smoke fixture project while the gate cleaned it up
# paged CRITICAL ("wave PR(s) closed without merge", project #7017, release
# run 38010697715).

_POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _poller_function(name: str) -> str:
	text = _POLLER.read_text(encoding="utf-8")
	match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
	assert match, name
	return match.group(0)


def _smoke_fixture_project_titles() -> list[str]:
	"""Every orchestrator project title the stable gate creates, run id filled in."""
	gate = (WORKFLOWS / "test-and-mark-stable.yml").read_text(encoding="utf-8")
	titles = []
	for match in re.finditer(r'(?:TITLE=|"title": )"(\[E2E [^"]+)"', gate):
		titles.append(match.group(1))
	# The decompose fixture's tracking title is model-written from the project
	# description, which starts with this marker; the orchestrator prefixes it.
	for match in re.finditer(r'PROJECT_DESC="(\[E2E [^\]]+\])', gate):
		titles.append("[Orchestrator] " + match.group(1) + " Update two smoke canary files in parallel")
	return [title.replace("${GITHUB_RUN_ID}", "38010697715").replace("${RUN_ID}", "38010697715") for title in titles]


def _is_smoke(title: str, labels_json: str = "[]") -> bool:
	script = _poller_function("tracking_issue_is_smoke_fixture") + '\ntracking_issue_is_smoke_fixture "$1" "$2"\n'
	result = subprocess.run(["bash", "-c", script, "smoke", title, labels_json], capture_output=True, text=True, timeout=30)
	return result.returncode == 0


def test_poller_detects_every_gate_fixture_project() -> None:
	titles = _smoke_fixture_project_titles()
	assert any("Review-Blocked Simulation" in title for title in titles), titles
	assert any(title.startswith("[Orchestrator] [E2E Orchestrate Smoke ") for title in titles), titles
	for title in titles:
		assert _is_smoke(title), title


def test_poller_does_not_treat_real_projects_as_smoke() -> None:
	for title in ("[Orchestrator] Unattended Claude pipeline completion", "E2E tests for the checkout flow", "[Orchestrator] Add [E2E] docs", "[Orchestrator] Add [E2E tests] for payments", "Fix [E2E Smoke Test] flake", "[Orchestrator] E2E tests for checkout", "[Orchestrator] [E2E tests] for payments", "[Orchestrator] [E2E Smoke Test] flake"):
		assert not _is_smoke(title, '[{"name":"ai:orchestrator-tracking"}]'), title
	# The decompose fixture's model-written title may drop the brackets.
	assert _is_smoke("[Orchestrator] E2E Orchestrate Smoke 38010697715: update two canary files")
	assert _is_smoke("[Orchestrator] anything", '[{"name":"ai:orchestrator-tracking"},{"name":"e2e-smoke-test"}]')
	assert not _is_smoke("[Orchestrator] anything", "not json")


def test_gate_labels_the_decompose_fixture_tracking_issue() -> None:
	"""The decompose fixture's tracking title is model-written and may drop the
	smoke marker, so the gate must also bind the e2e-smoke-test label."""
	gate = (WORKFLOWS / "test-and-mark-stable.yml").read_text(encoding="utf-8")
	start = gate.index('--field "project_description=${PROJECT_DESC}"')
	assert '--field "tracking_labels=e2e-smoke-test"' in gate[start:start + 200]
	assert "tracking_labels: ${{ inputs.tracking_labels }}" in (WORKFLOWS / "internal-orchestrate.yml").read_text(encoding="utf-8")
	assert _is_smoke("[Orchestrator] Update two smoke canary files in parallel", '[{"name":"ai:orchestrator-tracking"},{"name":"e2e-smoke-test"}]')


def _tg_notify_run(tracking_num: str, smoke_nums: str, level: str = "CRITICAL") -> subprocess.CompletedProcess:
	script = "\n".join([
		"set -euo pipefail",
		_poller_function("_smoke_fixture_alert_silenced"),
		_poller_function("tg_notify"),
		'_gh_url() { echo "https://github.com/o/r/$1"; }',
		'tg_send_tracked() { echo "SENT tracked=$1 level=$3"; }',
		'tg_send_msg() { echo "SENT untracked level=$2"; }',
		f"TRACKING_NUM={tracking_num}",
		f"SMOKE_FIXTURE_TRACKING_NUMS={smoke_nums!r}",
		f'tg_notify "Project #{tracking_num}: one or more wave PR(s) closed without merge" {level}',
	])
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def test_poller_tg_notify_is_silent_for_smoke_projects_only() -> None:
	silenced = _tg_notify_run("7017", " 7005 7017")
	assert silenced.returncode == 0, silenced.stderr
	assert "TG_NOTIFY_SMOKE_SILENCED tracking_issue=7017 level=CRITICAL" in silenced.stdout
	assert "SENT" not in silenced.stdout
	# A prefix of a smoke number is a different project.
	sent = _tg_notify_run("701", " 7005 7017")
	assert "SENT tracked=701 level=CRITICAL" in sent.stdout
	assert "SENT tracked=6664 level=CRITICAL" in _tg_notify_run("6664", "").stdout


def test_poller_builds_the_smoke_set_once_and_guards_direct_project_alerts() -> None:
	text = _POLLER.read_text(encoding="utf-8")
	build = text.index('SMOKE_FIXTURE_TRACKING_NUMS=""')
	assert build < text.index('for ((tidx=0; tidx<COUNT; tidx++)); do')
	assert 'SMOKE_FIXTURE_TRACKING_NUMS+=" ${smoke_candidate_num}"' in text
	assert text.count('if ! _smoke_fixture_alert_silenced "') == 5
	for message in (
		'tg_send_msg "${_final_merge_alert_msg}" "CRITICAL"',
		'MSG="✅ Project #${TRACKING_NUM} completed successfully."',
		'MSG="Project #${TRACKING_NUM} completed after validation pass',
		'MSG="Project #${TRACKING_NUM} completed! All waves merged and judge approved."',
	):
		site = text.index(message)
		assert "_smoke_fixture_alert_silenced" in text[site - 200:site + 400], message


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
