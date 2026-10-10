"""Workflow wiring for model-provider outage handling (issue #6633)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

from review_autofix_step_scripts import REVIEW_AUTOFIX_STEP_SCRIPTS, expanded_review_autofix_text

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
SWEEP = (REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml").read_text(encoding="utf-8")
CONSUMER = (REPO_ROOT / "workflow-templates" / "ai-review.yml").read_text(encoding="utf-8")
STAGE = (REPO_ROOT / "scripts" / "stage_workflow_support.sh").read_text(encoding="utf-8")
EXCLUSION = "env.AUTOFIX_FAILURE_REASON != 'provider_unavailable'"


def _step(text: str, name: str) -> str:
	start = text.index(f"      - name: {name}\n")
	end = text.find("\n      - name: ", start + 10)
	return text[start:] if end == -1 else text[start:end]


def _if(step: str) -> str:
	match = re.search(r"\n        if: (.*)\n", step)
	assert match, step[:200]
	return match.group(1)


def test_per_pr_failure_steps_skip_provider_outages() -> None:
	for name in (
		"Mark linked issues review-blocked (workflow failure)",
		"Force orchestrate poll after workflow failure review-blocked label",
		"Telegram failure",
		"Report autofix failure to workflow failure heal",
	):
		assert EXCLUSION in _if(_step(REVIEW, name)), name


def test_outage_record_step_runs_only_for_provider_outages() -> None:
	step = _step(REVIEW, "Record model-provider outage")
	assert _if(step).startswith("always() && env.AUTOFIX_FAILURE_REASON == 'provider_unavailable'")
	assert "continue-on-error: true" in step
	assert REVIEW.index("      - name: Assemble failure evidence\n") < REVIEW.index("      - name: Record model-provider outage\n") < REVIEW.index("      - name: Mark linked issues review-blocked (workflow failure)\n")
	expanded = expanded_review_autofix_text()
	record = _step(expanded, "Record model-provider outage")
	assert "open --repo \"${REPOSITORY}\" --kind outage" in record
	assert "OPENROUTER_API_KEY" not in record


def test_failure_evidence_classifies_and_comment_mentions_outage() -> None:
	expanded = expanded_review_autofix_text()
	evidence = _step(expanded, "Assemble failure evidence")
	assert "provider_outage.py" in evidence and "classify" in evidence
	assert '"${provider_outage_reason_args[@]}"' in evidence
	comment = _step(expanded, "Post review-blocked comment on PR (workflow failure)")
	assert "AI review/autofix paused — model provider unavailable" in comment
	assert "review-autofix-failure:v1" not in comment or "AUTOFIX_FAILURE_MARKER" in comment
	assert "${{" not in (REPO_ROOT / "scripts" / "review_autofix_step_failure_comment.sh").read_text(encoding="utf-8")


def test_editor_empty_noop_classifies_provider_outage() -> None:
	assert 'autofix_empty_failure_reason="provider_unavailable"' in REVIEW
	assert 'NOOP_BODY="${NOOP_BODY/produced no output — will retry/paused — model provider unavailable}"' in REVIEW


def test_claude_pool_capacity_step() -> None:
	assert "      - name: Resolve Claude credential\n        id: claude_pool\n" in REVIEW
	step = _step(REVIEW, "Record Claude pool capacity")
	assert "steps.claude_pool.outputs.reason == 'all_gated'" in _if(step)
	body = (REPO_ROOT / "scripts" / "review_autofix_step_claude_pool_capacity.sh").read_text(encoding="utf-8")
	assert "--kind capacity" in body and "PROVIDER_OUTAGE_CAPACITY_ALERTED=true" in body


def test_step_scripts_registered_and_staged() -> None:
	for name in (
		"Assemble failure evidence",
		"Record model-provider outage",
		"Mark linked issues review-blocked (workflow failure)",
		"Post review-blocked comment on PR (workflow failure)",
		"Record Claude pool capacity",
	):
		script, _mode = REVIEW_AUTOFIX_STEP_SCRIPTS[name]
		assert script in STAGE
		assert f'REVIEW_AUTOFIX_STEP_SCRIPT="${{SUPPORT_SCRIPTS_DIR:-}}/{script}"' in REVIEW
	assert re.search(r'^OPTIONAL_BOOTSTRAP_SCRIPTS="[^"]*\bprovider_outage\.py\b', STAGE, re.M)
	assert "workflow_failure_heal_autofix_report.sh provider_outage.py" in REVIEW


def test_sweep_probes_before_dispatch_and_honours_pause() -> None:
	checkout = SWEEP.index("      - name: Check out trusted CI recovery helper\n")
	probe = SWEEP.index("      - name: Provider outage probe and resume\n")
	enumerate_ = SWEEP.index("      - name: Enumerate open PRs and dispatch internal-review.yml\n")
	assert checkout < probe < enumerate_
	step = _step(SWEEP, "Provider outage probe and resume")
	assert "id: provider_outage" in step and "PROVIDER_OUTAGE_REVIEW_WORKFLOW: internal-review.yml" in step
	enum_step = _step(SWEEP, "Enumerate open PRs and dispatch internal-review.yml")
	assert "PROVIDER_OUTAGE_PAUSED: ${{ steps.provider_outage.outputs.paused }}" in enum_step
	assert enum_step.index("ci-cancelled-pr-snapshot.json") < enum_step.index("AUTOFIX_SWEEP_SKIP reason=provider_outage")
	assert "steps.provider_outage.outputs.paused != 'true'" in _if(_step(SWEEP, "Re-dispatch stale security holds"))


def test_consumer_job_is_least_privilege_and_trusted() -> None:
	job = CONSUMER[CONSUMER.index("  provider-outage-resume:\n"):]
	assert "github.event_name == 'schedule' && vars.PROVIDER_OUTAGE_RESUME_ENABLED != 'false'" in job
	assert "ref: stable" in job and "persist-credentials: false" in job
	assert "contents: read" in job and "id-token" not in job and "contents: write" not in job
	key_lines = [line for line in job.splitlines() if "OPENROUTER_API_KEY" in line]
	assert key_lines == ["          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}"]
	assert "PROVIDER_OUTAGE_REVIEW_WORKFLOW: ai-review.yml" in job
	assert "reason=gh_pat_missing" in job


def test_label_contract_and_reporter_skip() -> None:
	contract = json.loads((REPO_ROOT / ".github" / "ai" / "label_contract.v1.json").read_text(encoding="utf-8"))
	assert "ai:provider-outage" in json.dumps(contract)
	reporter = (REPO_ROOT / "scripts" / "workflow_failure_heal_autofix_report.sh").read_text(encoding="utf-8")
	assert 'log "skip reason=provider_unavailable pr=${PR}"' in reporter
	intake = (REPO_ROOT / "scripts" / "workflow_failure_heal_intake.sh").read_text(encoding="utf-8")
	assert intake.index("skip reason=provider_unavailable") < intake.index("# --- Fingerprint")


def test_judge_exit_zero_provider_skip_is_classified() -> None:
	judge = _step(REVIEW, "Review-blocked judge decision")
	assert "grep -E '^judge_skip_reason=' \"${GITHUB_OUTPUT}\"" in judge
	assert '[ "${rb_judge_skip_reason}" = "llm_failed" ]' in judge
	assert '[ "${rb_judge_skip_reason}" = "json_parse_failed" ]' in judge
	assert judge.index('if [ "${_judge_exit}" -eq 42 ]; then') < judge.index('echo "rb_judge_status=failed"')
	telegram = _step(REVIEW, "Telegram review-blocked judge decision")
	assert "steps.rb_judge.outputs.rb_judge_provider_unavailable != 'true'" in _if(telegram)


def test_sweep_probe_honours_dry_run() -> None:
	step = _step(SWEEP, "Provider outage probe and resume")
	assert "DRY_RUN: ${{ inputs.dry_run }}" in step
	assert 'sweep_mode_args=(--status-only)' in step


# --- Issue #7099: activation gaps left by PR #6655 -------------------------


def _job(text: str, name: str) -> str:
	start = text.index(f"  {name}:\n")
	match = re.search(r"\n  [a-z][a-z0-9_-]*:\n", text[start + 3:])
	return text[start:] if match is None else text[start:start + 3 + match.start()]


def _run_body(step: str) -> str:
	return textwrap.dedent(step[step.index("        run: |\n") + len("        run: |\n"):])


def test_consumer_security_hold_sweep_waits_for_outage_pause() -> None:
	outage = _job(CONSUMER, "provider-outage-resume")
	assert "    outputs:\n      paused: ${{ steps.provider_outage.outputs.paused }}\n" in outage
	assert "        id: provider_outage\n" in outage
	for reason in ("gh_pat_missing", "helper_missing"):
		branch = outage[outage.index(f"reason={reason}"):]
		assert branch.index('echo "paused=false" >> "$GITHUB_OUTPUT"') < branch.index("exit 0")
	sweep = _job(CONSUMER, "security-hold-sweep")
	assert "    needs: provider-outage-resume\n" in sweep
	condition = re.search(r"\n    if: (.*)\n", sweep).group(1)
	assert condition.startswith("${{ !cancelled() && ")
	assert "needs.provider-outage-resume.outputs.paused != 'true'" in condition
	assert "vars.SINGLE_ISSUE_SECURITY_PASS_ENABLED == 'true'" in condition


def test_exhaustion_label_step_exports_labelled_set() -> None:
	label = _step(REVIEW, "Mark linked issues review-blocked (autofix exhaustion)")
	assert "        id: mark_review_blocked_exhaustion\n" in label
	no_issues = label[label.index("No linked issues found"):]
	assert no_issues.index("labelled_issue_numbers_resolved=true") < no_issues.index("exit 0")
	loop = label[label.index("labelled=()"):]
	assert 'SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME=""' in loop
	assert 'echo "labelled_issue_numbers=${labelled_csv}"' in loop
	assert label.count('SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME="applied"') == 2
	marker = _step(REVIEW, "Mark outage-caused review-blocked labels (autofix exhaustion)")
	assert "REVIEW_BLOCKED_LABELLED_ISSUES: ${{ steps.mark_review_blocked_exhaustion.outputs.labelled_issue_numbers }}" in marker
	assert "REVIEW_BLOCKED_LABELLED_RESOLVED: ${{ steps.mark_review_blocked_exhaustion.outputs.labelled_issue_numbers_resolved }}" in marker
	assert marker.index("REVIEW_BLOCKED_LABELLED_RESOLVED:-") < marker.index("reason=labelled_set_unavailable") < marker.index("LINKED_ISSUES_JSON")


def _run_marker(tmp_path: Path, **env: str) -> tuple[list[str], str]:
	support = tmp_path / "support"
	support.mkdir()
	log = tmp_path / "calls.txt"
	(support / "provider_outage.py").write_text(
		"import sys\n"
		f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n",
		encoding="utf-8",
	)
	body = _run_body(_step(REVIEW, "Mark outage-caused review-blocked labels (autofix exhaustion)"))
	run_env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"SUPPORT_SCRIPTS_DIR": str(support),
		"REPOSITORY": "o/r",
		"PR_NUMBER": "77",
		"RB_JUDGE_PROVIDER_STATUS": "402",
		**env,
	}
	result = subprocess.run(["bash", "-c", body], env=run_env, capture_output=True, text=True, check=False, timeout=60)
	assert result.returncode == 0, result.stderr
	calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
	return calls, result.stdout


def _marked(calls: list[str]) -> list[str]:
	return [call.split("--item ")[1].split()[0] for call in calls if call.startswith("mark-label")]


def test_marker_step_marks_exactly_the_labelled_set(tmp_path: Path) -> None:
	calls, _ = _run_marker(tmp_path, REVIEW_BLOCKED_LABELLED_ISSUES="12,34", REVIEW_BLOCKED_LABELLED_RESOLVED="true", LINKED_ISSUES_JSON="[]")
	assert _marked(calls) == ["12", "34"]
	assert any(call.startswith("open ") for call in calls)


def test_marker_step_empty_labelled_set_still_opens_tracker(tmp_path: Path) -> None:
	calls, _ = _run_marker(tmp_path, REVIEW_BLOCKED_LABELLED_ISSUES="", REVIEW_BLOCKED_LABELLED_RESOLVED="true")
	assert _marked(calls) == []
	assert any(call.startswith("open ") for call in calls)


def test_marker_step_unresolved_set_uses_legacy_derivation(tmp_path: Path) -> None:
	calls, out = _run_marker(tmp_path, LINKED_ISSUES_JSON="[]", LINKED_ISSUE_FALLBACK_NUMBERS_JSON="[]")
	assert _marked(calls) == ["77"]
	assert "reason=labelled_set_unavailable" in out


def test_marker_step_drops_non_numeric_items(tmp_path: Path) -> None:
	calls, _ = _run_marker(tmp_path, REVIEW_BLOCKED_LABELLED_ISSUES="12,x;rm", REVIEW_BLOCKED_LABELLED_RESOLVED="true")
	assert _marked(calls) == ["12"]


_FAKE_GH = """#!/usr/bin/env bash
case "$*" in
  "label create"*) exit 0 ;;
  *"-X PUT"*) cat >/dev/null; [ "${FAKE_PUT:-ok}" = ok ] ;;
  *"-X POST"*) [ "${FAKE_POST:-ok}" = ok ] ;;
  *--paginate*) echo '[]' ;;
  *) exit 0 ;;
esac
"""


def _label_outcome(tmp_path: Path, args: str, **env: str) -> tuple[int, str]:
	scripts = tmp_path / "scripts"
	scripts.mkdir(exist_ok=True)
	(scripts / "label_helpers.sh").write_text((REPO_ROOT / "scripts" / "label_helpers.sh").read_text(encoding="utf-8"), encoding="utf-8")
	bindir = tmp_path / "bin"
	bindir.mkdir(exist_ok=True)
	gh = bindir / "gh"
	gh.write_text(_FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	script = (
		f'source "{scripts}/label_helpers.sh"\n'
		f"set_issue_phase_label_resilient {args}\n"
		'rc=$?\nprintf "%s %s" "$rc" "${SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME}"\n'
	)
	run_env = {"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "GITHUB_REPOSITORY": "o/r", **env}
	result = subprocess.run(["bash", "-c", script], env=run_env, capture_output=True, text=True, check=False, timeout=60)
	rc, _, outcome = result.stdout.partition(" ")
	return int(rc), outcome


def test_label_helper_reports_outcome_without_changing_return_code(tmp_path: Path) -> None:
	assert _label_outcome(tmp_path, "5 ai:review-blocked") == (0, "applied")
	assert _label_outcome(tmp_path, "5 ai:review-blocked", FAKE_PUT="fail") == (0, "applied")
	assert _label_outcome(tmp_path, "5 ai:review-blocked", FAKE_PUT="fail", FAKE_POST="fail") == (0, "failed")
	assert _label_outcome(tmp_path, "''") == (0, "skipped")
