#!/usr/bin/env python3
"""Regression tests for the cross-cycle needs_fixes -> harness_error escalation.

The validate-diagnose prompt was inverted (prefer `needs_fixes` when in doubt)
to give the consumer-repo editor more autonomy to fix failures directly. The
safety net is an orchestrator-side escalation: if the same fix-up proposal
(fingerprinted from sorted fix_issues[].title) fails across 3 consecutive
cycles, we promote to `harness_error` so the workflow surfaces a human-visible
signal instead of looping indefinitely.

These tests lock the mechanism in place by asserting on the shell structure,
not runtime behavior, so they stay fast and deterministic.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
DIAGNOSE_PROMPT = REPO_ROOT / "prompts" / "mode-validate-diagnose.txt"


def _validate_process_text() -> str:
	return VALIDATE_PROCESS.read_text(encoding="utf-8")


def _diagnose_prompt_text() -> str:
	return DIAGNOSE_PROMPT.read_text(encoding="utf-8")


JUDGE_PROMPT = REPO_ROOT / "prompts" / "mode-judge.txt"
ORCHESTRATE_POLL_JUDGE_PROMPT = REPO_ROOT / "prompts" / "mode-orchestrate-poll-judge.txt"


def _judge_prompt_text() -> str:
	return JUDGE_PROMPT.read_text(encoding="utf-8")


def _orchestrate_poll_judge_prompt_text() -> str:
	return ORCHESTRATE_POLL_JUDGE_PROMPT.read_text(encoding="utf-8")


def _extract_needs_fixes_branch() -> str:
	text = _validate_process_text()
	match = re.search(
		r'  needs_fixes\)\n(?P<branch>.*?)\n    ;;\n\n  harness_error\)',
		text,
		re.DOTALL,
	)
	if not match:
		raise AssertionError("could not extract needs_fixes branch from validate_process.sh")
	return match.group("branch")


def _extract_harness_error_branch() -> str:
	text = _validate_process_text()
	match = re.search(
		r'  harness_error\)\n(?P<branch>.*?)\n    ;;\n\n  infeasible\)',
		text,
		re.DOTALL,
	)
	if not match:
		raise AssertionError("could not extract harness_error branch from validate_process.sh")
	return match.group("branch")


def test_fingerprint_is_sha256_of_sorted_fix_issue_titles() -> None:
	"""Q5=A: fingerprint hashes sort(fix_issues[].title), 16-char hex prefix."""
	text = _validate_process_text()
	assert "FAILURE_FINGERPRINT=" in text, (
		"expected FAILURE_FINGERPRINT assignment in validate_process.sh"
	)
	assert "sort_by(.title" in text, (
		"expected jq sort_by(.title) when computing the failure fingerprint"
	)
	assert "sha256sum" in text, (
		"expected sha256sum in the fingerprint pipeline"
	)
	assert "cut -c1-16" in text, (
		"expected 16-char hex prefix of the sha256 fingerprint"
	)


def test_escalation_gated_on_cycle_ge_3_and_hits_ge_2() -> None:
	"""Q7=B: escalate only from the 3rd consecutive cycle onward with the same fingerprint."""
	text = _validate_process_text()
	# Cycle gate
	assert re.search(r'\[\s*"\$\{VALIDATION_CYCLE\}"\s*-ge\s*3\s*\]', text), (
		"expected VALIDATION_CYCLE >= 3 gate in escalation block (Q7=B)"
	)
	# Prior-hits gate
	assert re.search(r'\[\s*"\$\{PRIOR_FINGERPRINT_HITS\}"\s*-ge\s*2\s*\]', text), (
		"expected PRIOR_FINGERPRINT_HITS >= 2 gate in escalation block"
	)
	# Promotion side-effect
	assert 'DIAG_STATUS="harness_error"' in text, (
		"expected DIAG_STATUS to be re-assigned to harness_error on escalation"
	)
	assert 'ESCALATED_FROM_NEEDS_FIXES=true' in text, (
		"expected ESCALATED_FROM_NEEDS_FIXES flag to be set on escalation"
	)


def test_fingerprint_marker_embedded_in_needs_fixes_tracking_comment() -> None:
	"""Q6=A: the tracking comment includes an HTML-comment fingerprint marker
	so the next cycle's PRIOR_COMMENTS fetch can see it."""
	branch = _extract_needs_fixes_branch()
	assert "validation-failure-fingerprint:" in branch, (
		"expected `validation-failure-fingerprint:` HTML-comment marker in the "
		"needs_fixes tracking comment body"
	)
	assert "${FAILURE_FINGERPRINT}" in branch, (
		"expected FAILURE_FINGERPRINT variable to be interpolated into the marker"
	)
	assert "${VALIDATION_CYCLE}" in branch, (
		"expected VALIDATION_CYCLE to be recorded in the fingerprint marker"
	)


def test_harness_error_branch_handles_escalated_case() -> None:
	"""Q9=B: when escalated from needs_fixes, prefer LLM-provided harness_fixes
	if present; otherwise use a templated explanation."""
	branch = _extract_harness_error_branch()
	assert 'ESCALATED_FROM_NEEDS_FIXES' in branch, (
		"expected harness_error branch to check ESCALATED_FROM_NEEDS_FIXES flag"
	)
	assert "HARNESS_FIXES_FROM_LLM=" in branch, (
		"expected LLM-provided harness_fixes extraction on escalation (Q9=B)"
	)
	assert "Cross-cycle escalation" in branch, (
		"expected escalation annotation in the harness_fixes text"
	)


def test_harness_error_comments_include_current_run_reference() -> None:
	"""Both harness-error paths expose the current validation run when available."""
	branch = _extract_harness_error_branch()
	assert 'if [ -n "${GITHUB_RUN_ID:-}" ]; then' in branch
	assert 'HARNESS_RUN_REFERENCE=$\'\\n\\n\'"Run: $(_gh_url "actions/runs/${GITHUB_RUN_ID}")"' in branch
	assert branch.count("${HARNESS_FIXES}${HARNESS_RUN_REFERENCE}") == 2


def test_prior_fingerprint_scan_uses_existing_prior_comments_fetch() -> None:
	"""Q6=A: no new GitHub API call — reuse PRIOR_COMMENTS already fetched
	at the top of validate_process.sh for the cycle-N LLM context."""
	text = _validate_process_text()
	# The PRIOR_COMMENTS variable must be defined before the escalation block
	# (which relies on it), and the escalation block must read from it.
	prior_comments_def = text.find('PRIOR_COMMENTS="$(gh_retry gh api')
	escalation_read = text.find('<!-- validation-failure-fingerprint: ${FAILURE_FINGERPRINT} cycle:')
	assert prior_comments_def != -1, (
		"expected PRIOR_COMMENTS fetch to exist (validate_process.sh:~1678)"
	)
	assert escalation_read != -1, (
		"expected escalation block to grep PRIOR_COMMENTS for fingerprint marker"
	)
	assert prior_comments_def < escalation_read, (
		"escalation block must run after PRIOR_COMMENTS is populated"
	)


def test_diagnose_prompt_inverts_when_in_doubt_default() -> None:
	"""Q8: prefer needs_fixes when in doubt; reserve harness_error for
	coding-workflows-owned files."""
	text = _diagnose_prompt_text()
	# Old default should be gone
	assert "When in doubt, prefer `harness_error`" not in text, (
		"old `prefer harness_error` default should have been removed"
	)
	# New rule markers
	assert "Classify as `harness_error` ONLY when" in text, (
		"expected new `ONLY when` rule for harness_error in the classification tree"
	)
	assert "coding-workflows" in text, (
		"expected coding-workflows ownership language in the classification tree"
	)
	# needs_fixes should cover everything else
	assert "Classify as `needs_fixes` for EVERYTHING ELSE" in text, (
		"expected `needs_fixes` to be the catch-all per Q8"
	)


def test_diagnose_prompt_schema_allows_harness_fixes_alongside_fix_issues() -> None:
	"""Q9=B: when status=needs_fixes, the LLM may also populate harness_fixes
	as a fallback hint used by cross-cycle escalation."""
	text = _diagnose_prompt_text()
	assert "fallback hint" in text or "fallback" in text, (
		"expected schema docstring to mention the harness_fixes fallback role"
	)
	# Neither schema keyword changed; the semantics change is prose-only.
	assert '"harness_fixes": "string"' in text, (
		"harness_fixes must remain in the schema"
	)




def test_audit_gate_prompt_contract_forbids_unsatisfiable_wording() -> None:
	"""Issue #1668: lock the audit-gate carve-out wording in diagnose + judge prompts."""
	diagnose = _diagnose_prompt_text().lower().partition(
		"audit-gate carve-out (`npm run audit:ci`)"
	)[2]
	judge = _judge_prompt_text().lower().partition(
		"audit-gate carve-out (`npm run audit:ci`)"
	)[2]
	assert diagnose, "diagnose prompt must include the audit-gate carve-out heading"
	assert judge, "judge prompt must include the audit-gate carve-out heading"
	for forbidden in (
		"make npm audit deterministic",
		"match the validator's normalization exactly",
	):
		assert forbidden in diagnose, (
			"diagnose prompt must explicitly forbid unsatisfiable audit-gate wording"
		)
		assert forbidden in judge, (
			"judge prompt must explicitly forbid unsatisfiable audit-gate wording"
		)


def test_audit_gate_prompt_contract_requires_concrete_supported_remediation() -> None:
	"""Issue #1668: audit-gate follow-ups must name the concrete supported path."""
	diagnose = _diagnose_prompt_text().lower().partition(
		"audit-gate carve-out (`npm run audit:ci`)"
	)[2]
	judge = _judge_prompt_text().lower().partition(
		"audit-gate carve-out (`npm run audit:ci`)"
	)[2]
	assert diagnose, "diagnose prompt must include the audit-gate carve-out heading"
	assert judge, "judge prompt must include the audit-gate carve-out heading"
	for marker in (
		"npm run audit:ci -- --write",
		"security/dependency-audit-allowlist.json",
		"scripts/security/check-npm-audit.js",
		"prefer ghsa",
		"fallback cve",
	):
		assert marker in diagnose, (
			"diagnose prompt must require concrete canonical audit remediation markers"
		)
		assert marker in judge, (
			"judge prompt must require concrete canonical audit remediation markers"
		)


def test_judge_prompts_treat_harness_errors_as_infrastructure() -> None:
	"""Harness-only validation failures must be framed as infrastructure defects."""
	for text in (_judge_prompt_text(), _orchestrate_poll_judge_prompt_text()):
		assert "raw_status=harness_error" in text, (
			"judge prompt must mention raw_status=harness_error explicitly"
		)
		assert "ai:harness-broken" in text, (
			"judge prompt must mention the ai:harness-broken label explicitly"
		)
		assert "harness/infrastructure failure" in text, (
			"judge prompt must frame harness-only validation failures as infrastructure defects"
		)
		assert "application regression" in text, (
			"judge prompt must distinguish harness failures from application regressions"
		)

# ---------------------------------------------------------------------------
# Project #7913 p4: VALIDATION_CYCLE derived from the newest trusted
# "Runtime validation dispatched" tracking comment when it is unset.
# ---------------------------------------------------------------------------

class SkipTest(unittest.SkipTest):
	pass


DISPATCH_HEADER = "## \U0001f9ea Runtime validation dispatched"
PIPELINE_LOGIN = "pipeline-bot"

CYCLE_STUBS = textwrap.dedent(
	"""\
	gh_retry() { "$@"; }
	gh() {
		printf '%s\\n' "$*" >> "${GH_LOG}"
		case "$*" in
			"api user"*) printf '%s\\n' "${STUB_LOGIN:-}" ;;
			*comments*) cat "${STUB_COMMENTS_FILE:-/dev/null}" ;;
		esac
	}
	is_tracking_run() { [ "${TRACKING_ISSUE_NUM}" -gt 0 ]; }
	compact_validation_summary_value() { printf '%s' "$1" | tr '\\r\\n' '  '; }
	"""
)


def _need_tools() -> None:
	if not shutil.which("jq") or not shutil.which("bash"):
		raise SkipTest("jq or bash is not installed")


def _extract(pattern: str, what: str) -> str:
	match = re.search(pattern, _validate_process_text(), re.DOTALL | re.MULTILINE)
	if not match:
		raise AssertionError(f"{what} not found in validate_process.sh")
	return match.group(0)


def _owner_block() -> str:
	return _extract(
		r"^# BEGIN validation owner routing \(project #7913 p1c\)\n.*?^# END validation owner routing\n",
		"owner routing block",
	)


def _cycle_block() -> str:
	return _extract(
		r"^# BEGIN validation cycle derivation \(project #7913 p4\)\n.*?^# END validation cycle derivation\n",
		"cycle derivation block",
	)


def _escalation_block() -> str:
	return _extract(
		r'^if \[ "\$\{DIAG_STATUS\}" = "needs_fixes" \] \\\n.*?^fi\n',
		"cross-cycle escalation block",
	)


def _dispatch_item(login: str, cycle_line: str) -> str:
	body = f"{DISPATCH_HEADER}\n\n{cycle_line}\n- Workflow: `ai-validate.yml`"
	return json.dumps({"login": login, "body": body})


def _run_cycle(items: list[str], env: dict[str, str] | None = None, after: str = "") -> tuple[subprocess.CompletedProcess, str]:
	root = Path(tempfile.mkdtemp(prefix="cycle-derivation-"))
	try:
		runtime = root / "runtime"
		runtime.mkdir()
		comments = root / "comments.jsonl"
		comments.write_text("".join(item + "\n" for item in items), encoding="utf-8")
		gh_log = root / "gh.log"
		gh_log.write_text("", encoding="utf-8")
		full_env = {
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"HOME": str(root),
			"RUNTIME_DIR": str(runtime),
			"GITHUB_REPOSITORY": "acme/widgets",
			"TRACKING_ISSUE_NUM": "7",
			"GH_LOG": str(gh_log),
			"STUB_LOGIN": PIPELINE_LOGIN,
			"STUB_COMMENTS_FILE": str(comments),
			"VALIDATION_CYCLE": "1",
			"VALIDATION_CYCLE_EXPLICIT": "false",
			"VALIDATION_CYCLE_INVALID_ENV": "false",
		}
		full_env.update(env or {})
		script = root / "run.sh"
		script.write_text(
			"set -euo pipefail\n" + CYCLE_STUBS + _owner_block() + _cycle_block()
			+ 'echo "RESULT_CYCLE=${VALIDATION_CYCLE}"\n' + after,
			encoding="utf-8",
		)
		proc = subprocess.run(["bash", str(script)], env=full_env, capture_output=True, text=True, timeout=120)
		return proc, gh_log.read_text(encoding="utf-8")
	finally:
		shutil.rmtree(root, ignore_errors=True)


def _result_cycle(proc: subprocess.CompletedProcess) -> str:
	assert proc.returncode == 0, proc.stderr + proc.stdout
	for line in proc.stdout.splitlines():
		if line.startswith("RESULT_CYCLE="):
			return line.split("=", 1)[1]
	raise AssertionError("no RESULT_CYCLE line: " + proc.stdout)


def test_cycle_derivation_defaulting_records_explicit_before_default() -> None:
	text = _validate_process_text()
	explicit = text.find('VALIDATION_CYCLE_EXPLICIT="true"')
	default = text.find('VALIDATION_CYCLE="${VALIDATION_CYCLE:-1}"')
	assert explicit != -1 and default != -1
	assert explicit < default, "explicit-value flag must be recorded before the :-1 default"


def test_cycle_derivation_runs_before_prior_comments_fetch() -> None:
	text = _validate_process_text()
	call = text.find("\nvalidation_cycle_resolve || true\n")
	owner_end = text.find("# END validation owner routing\n")
	prior = text.find('PRIOR_COMMENTS="$(gh_retry gh api')
	assert call != -1 and owner_end != -1 and prior != -1
	assert owner_end < call < prior
	# tests/test_validate_process_template_mode.py extracts everything from
	# write_result_files() up to "\n\nemit_phase_failure_marker()" and runs it
	# under set -u; the top-level resolver call must stay outside that slice.
	marker = text.find("\n\nemit_phase_failure_marker()")
	assert marker != -1 and marker < call, "cycle derivation must follow emit_phase_failure_marker()"


def test_cycle_derived_from_trusted_dispatch_comment() -> None:
	_need_tools()
	proc, _ = _run_cycle([_dispatch_item(PIPELINE_LOGIN, "- Cycle: 2"), _dispatch_item(PIPELINE_LOGIN, "- Cycle: 3")])
	assert _result_cycle(proc) == "3"
	assert "VALIDATION_CYCLE_SOURCE source=dispatch_comment cycle=3" in proc.stdout


def test_explicit_cycle_env_wins_without_comment_read() -> None:
	_need_tools()
	proc, gh_log = _run_cycle(
		[_dispatch_item(PIPELINE_LOGIN, "- Cycle: 3")],
		env={"VALIDATION_CYCLE": "5", "VALIDATION_CYCLE_EXPLICIT": "true"},
	)
	assert _result_cycle(proc) == "5"
	assert "VALIDATION_CYCLE_SOURCE source=env cycle=5" in proc.stdout
	assert "comments" not in gh_log and "api user" not in gh_log, gh_log


def test_invalid_explicit_cycle_is_not_derived() -> None:
	_need_tools()
	proc, gh_log = _run_cycle(
		[_dispatch_item(PIPELINE_LOGIN, "- Cycle: 3")],
		env={"VALIDATION_CYCLE": "1", "VALIDATION_CYCLE_EXPLICIT": "true", "VALIDATION_CYCLE_INVALID_ENV": "true"},
	)
	assert _result_cycle(proc) == "1"
	assert "source=default cycle=1 reason=invalid_env" in proc.stdout
	assert "comments" not in gh_log


def test_untrusted_dispatch_comments_are_ignored() -> None:
	_need_tools()
	proc, _ = _run_cycle([_dispatch_item(PIPELINE_LOGIN, "- Cycle: 2"), _dispatch_item("outsider", "- Cycle: 9")])
	assert _result_cycle(proc) == "2"
	assert "source=dispatch_comment cycle=2" in proc.stdout

	proc, _ = _run_cycle([_dispatch_item("outsider", "- Cycle: 9")])
	assert _result_cycle(proc) == "1"
	assert "source=default cycle=1 reason=no_dispatch_comment" in proc.stdout


def test_malformed_cycle_line_falls_back_to_one() -> None:
	_need_tools()
	for bad in ("- Cycle: abc", "- Cycle: 0", "- Cycle: 9999999"):
		proc, _ = _run_cycle([_dispatch_item(PIPELINE_LOGIN, "- Cycle: 4"), _dispatch_item(PIPELINE_LOGIN, bad)])
		assert _result_cycle(proc) == "1", bad
		assert "source=default cycle=1 reason=malformed_cycle" in proc.stdout, bad


def test_cycle_login_unavailable_falls_back_to_one() -> None:
	_need_tools()
	proc, _ = _run_cycle([_dispatch_item(PIPELINE_LOGIN, "- Cycle: 3")], env={"STUB_LOGIN": ""})
	assert _result_cycle(proc) == "1"
	assert "source=default cycle=1 reason=login_unavailable" in proc.stdout


def test_cycle_not_tracking_run_makes_no_api_call() -> None:
	_need_tools()
	proc, gh_log = _run_cycle([_dispatch_item(PIPELINE_LOGIN, "- Cycle: 3")], env={"TRACKING_ISSUE_NUM": "0"})
	assert _result_cycle(proc) == "1"
	assert "reason=not_tracking_run" in proc.stdout
	assert gh_log == ""


def test_escalation_fires_at_derived_cycle_three() -> None:
	_need_tools()
	after = textwrap.dedent(
		"""\
		DIAG_STATUS="needs_fixes"
		FAILURE_FINGERPRINT="abc"
		PRIOR_FINGERPRINT_HITS=0
		ESCALATED_FROM_NEEDS_FIXES=false
		PRIOR_COMMENTS='<!-- validation-failure-fingerprint: abc cycle: 1 -->
		<!-- validation-failure-fingerprint: abc cycle: 2 -->'
		"""
	) + _escalation_block() + 'echo "RESULT_STATUS=${DIAG_STATUS} ESCALATED=${ESCALATED_FROM_NEEDS_FIXES}"\n'
	proc, _ = _run_cycle([_dispatch_item(PIPELINE_LOGIN, "- Cycle: 3")], after=after)
	assert _result_cycle(proc) == "3"
	assert "RESULT_STATUS=harness_error ESCALATED=true" in proc.stdout, proc.stdout
	assert "Cross-cycle escalation" in proc.stdout


def main() -> int:
	failed = 0
	for name in sorted(n for n in globals() if n.startswith("test_")):
		try: globals()[name]()
		except SkipTest as exc:
			print(f"  SKIP  {name}: {exc}")
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
