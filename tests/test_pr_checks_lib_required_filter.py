#!/usr/bin/env python3
"""Tests for the shared PR check-runs merge gate (scripts/pr_checks_lib.sh).

scripts/pr_checks_lib.sh hosts `_pr_checks_completed` and
`_pr_required_check_names_for_base`, the single gate shared by
scripts/orchestrate_poll_process.sh (the orchestrator's merge gates) and
scripts/review_rb_judge.sh (the review-blocked judge's merge_with_followup
gate). Before this library, the two gates carried independent copies and
drifted: the orchestrator filtered to required checks while the rb_judge
blocked on ANY failing check-run. That asymmetry deadlocked the
review-blocked-judge merge whenever a non-required / environmental check
(e.g. CodeQL when code scanning is disabled at the repo level) was
permanently red — the judge approved, the stricter gate refused, the issue
stayed ai:review-blocked, and stall recovery re-fired forever.

These tests pin the required-checks behaviour (the deadlock fix), the
self-run exclusion in BOTH filter branches, the reason side-channel, and the
drift guard that keeps the library default identical to the orchestrator's
ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _page(check_runs: list[dict]) -> str:
	"""Production --paginate --slurp shape: an array of page objects."""
	return json.dumps([{"check_runs": check_runs}])


def _run(
	body: str,
	*,
	runs_json: str = "{}",
	prot_json: str = "",
	pr_json: str = "{}",
	jq_fail_uri_encode: bool = False,
	env: dict[str, str] | None = None,
):
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")
	# Stub gh_retry/_safe_gh_jq so the gate reads fixtures instead of hitting
	# the network. The router keys off the API path the helper requests.
	jq_wrapper = ""
	if jq_fail_uri_encode:
		jq_wrapper = """
jq() {
  if [ "$#" -ge 2 ] && [ "$1" = "-sRr" ] && [ "$2" = "@uri" ]; then
    return 127
  fi
  command jq "$@"
}
"""
	preamble = f"""
set -uo pipefail
{jq_wrapper}
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
  case "$*" in
    *"/protection"*)
      if [ -n "${{PROT_EXPECT_PATH:-}}" ] && [ "$*" != "${{PROT_EXPECT_PATH}}" ]; then
        printf '%s' ''
      else
        printf '%s' "${{PROT_JSON}}"
      fi
      ;;
    *"/check-runs"*) printf '%s' "${{RUNS_JSON}}" ;;
    *"/pulls/"*) printf '%s' "${{PR_JSON}}" ;;
    *) printf '%s' '{{}}' ;;
  esac
}}
source {str(LIB)!r}
"""
	full_env = {
		"PATH": __import__("os").environ.get("PATH", "/usr/bin:/bin"),
		"PYTHONDONTWRITEBYTECODE": "1",
		"RUNS_JSON": runs_json,
		"PROT_JSON": prot_json,
		"PR_JSON": pr_json,
	}
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", preamble + body],
		env=full_env,
		capture_output=True,
		text=True,
		timeout=60,
	)


def _gate(
	*,
	runs_json: str,
	args: str,
	prot_json: str = "",
	pr_json: str = "{}",
	jq_fail_uri_encode: bool = False,
	env: dict[str, str] | None = None,
) -> tuple[int, str]:
	"""Run _pr_checks_completed and return (rc, PR_CHECKS_LAST_REASON)."""
	body = (
		f'_pr_checks_completed {args} >/dev/null 2>&1; rc=$?; '
		'printf "%s\\n" "${rc}:${PR_CHECKS_LAST_REASON}"'
	)
	res = _run(
		body,
		runs_json=runs_json,
		prot_json=prot_json,
		pr_json=pr_json,
		jq_fail_uri_encode=jq_fail_uri_encode,
		env=env,
	)
	assert res.returncode == 0, res.stderr
	rc_str, _, reason = res.stdout.strip().partition(":")
	return int(rc_str), reason


REPO_ENV = {"PR_CHECKS_REPOSITORY": "owner/repo"}


# ---------------------------------------------------------------------------
# The deadlock fix: required-filter lets non-required failures through.
# ---------------------------------------------------------------------------


def test_non_required_failing_check_does_not_block() -> None:
	"""A FAILED non-required/advisory check (e.g. CodeQL with code scanning
	disabled) must NOT block when a base ref is passed (required filter).
	This is the core deadlock fix."""
	runs = _page([
		{"name": "CI", "status": "completed", "conclusion": "success"},
		{"name": "Security CodeQL JS-TS", "status": "completed", "conclusion": "failure"},
	])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=REPO_ENV)
	assert rc == 0 and reason == "ok", (rc, reason)


def test_required_failing_check_blocks() -> None:
	"""A FAILED required check still blocks under the required filter."""
	runs = _page([{"name": "CI", "status": "completed", "conclusion": "failure"}])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_pending_check_always_blocks_even_if_not_required() -> None:
	"""Pending check-runs always block (an in-flight workflow must not race
	the merge), regardless of whether they are in the required set."""
	runs = _page([{"name": "Optional", "status": "in_progress", "conclusion": None}])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_legacy_two_arg_mode_blocks_on_any_failure() -> None:
	"""Omitting the base ref (2-arg call) preserves the legacy
	block-on-ANY-failure behaviour for backward compatibility."""
	runs = _page([
		{"name": "CI", "status": "completed", "conclusion": "success"},
		{"name": "Security CodeQL JS-TS", "status": "completed", "conclusion": "failure"},
	])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234"', env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


# ---------------------------------------------------------------------------
# Sentinels + branch-protection precedence.
# ---------------------------------------------------------------------------


def test_allow_all_empty_sentinel_passes() -> None:
	runs = _page([{"name": "CI", "status": "completed", "conclusion": "failure"}])
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "main"',
		env={**REPO_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""},
	)
	assert rc == 0 and reason == "allow_all", (rc, reason)


def test_star_sentinel_blocks_on_any_failure() -> None:
	runs = _page([{"name": "advisory", "status": "completed", "conclusion": "failure"}])
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "main"',
		env={**REPO_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": "*"},
	)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_branch_protection_contexts_take_precedence() -> None:
	"""When the base ref is protected, its required_status_checks.contexts
	define the required set (server-side truth)."""
	runs = _page([{"name": "tests/integration", "status": "completed", "conclusion": "failure"}])
	prot = json.dumps({"required_status_checks": {"contexts": ["CI", "tests/integration"]}})
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', prot_json=prot, env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_branch_protection_lookup_url_encodes_slash_base_ref() -> None:
	"""Slash-containing base refs must be URL-encoded for the protection
	lookup or GitHub returns 404 and the gate falls back to defaults."""
	runs = _page([{"name": "deploy-check", "status": "completed", "conclusion": "failure"}])
	prot = json.dumps({"required_status_checks": {"contexts": ["deploy-check"]}})
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "release/v1"',
		prot_json=prot,
		env={
			**REPO_ENV,
			"PROT_EXPECT_PATH": "repos/owner/repo/branches/release%2Fv1/protection",
		},
	)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_branch_protection_lookup_falls_back_to_python_encoder() -> None:
	"""If the jq @uri helper is unavailable, the slash-containing base ref
	must still be URL-encoded before the protection lookup."""
	runs = _page([{"name": "deploy-check", "status": "completed", "conclusion": "failure"}])
	prot = json.dumps({"required_status_checks": {"contexts": ["deploy-check"]}})
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "release/v1"',
		prot_json=prot,
		jq_fail_uri_encode=True,
		env={
			**REPO_ENV,
			"PROT_EXPECT_PATH": "repos/owner/repo/branches/release%2Fv1/protection",
		},
	)
	assert rc == 1 and reason == "blocking", (rc, reason)


# ---------------------------------------------------------------------------
# Self-run exclusion in the required-set branch (needed by review_rb_judge.sh).
# ---------------------------------------------------------------------------


def test_self_run_excluded_in_required_filter() -> None:
	"""The rb_judge's own still-in_progress host job (a check-run on the
	polled SHA) must be excluded when PR_CHECKS_SELF_RUN_ID is set, even
	under the required filter — otherwise the gate self-deadlocks."""
	runs = _page([
		{
			"name": "review / codex-agent",
			"status": "in_progress",
			"conclusion": None,
			"details_url": "https://github.com/owner/repo/actions/runs/999/job/9",
		},
	])
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "main"',
		env={**REPO_ENV, "PR_CHECKS_SELF_RUN_ID": "999"},
	)
	assert rc == 0 and reason == "ok", (rc, reason)


def test_self_run_exclusion_is_narrow() -> None:
	"""Only the matching self-run is excluded; a second genuine pending
	check on the same SHA still blocks."""
	runs = _page([
		{
			"name": "review / codex-agent",
			"status": "in_progress",
			"conclusion": None,
			"details_url": "https://github.com/owner/repo/actions/runs/999/job/9",
		},
		{
			"name": "CI",
			"status": "in_progress",
			"conclusion": None,
			"details_url": "https://github.com/owner/repo/actions/runs/111/job/1",
		},
	])
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "main"',
		env={**REPO_ENV, "PR_CHECKS_SELF_RUN_ID": "999"},
	)
	assert rc == 1 and reason == "blocking", (rc, reason)


# ---------------------------------------------------------------------------
# API-error fail-closed + reason side-channel.
# ---------------------------------------------------------------------------


def test_api_error_fails_closed_with_query_failed_reason() -> None:
	"""An empty-object '{}' check-runs payload (API error fallback) matches
	neither jq branch and must fail closed with reason=query_failed."""
	rc, reason = _gate(runs_json="{}", args='5 "abc1234" "main"', env=REPO_ENV)
	assert rc == 1 and reason == "query_failed", (rc, reason)


def test_unresolved_head_sha_reason() -> None:
	"""When the head SHA cannot be resolved (PR JSON has none), the gate
	fails closed with reason=unresolved_head_sha."""
	# No head_sha arg → helper fetches PR JSON, which lacks .head.sha.
	rc, reason = _gate(runs_json="{}", args='5 "" "main"', pr_json="{}", env=REPO_ENV)
	assert rc == 1 and reason == "unresolved_head_sha", (rc, reason)


def test_reason_side_channel_survives_env_prefixed_call() -> None:
	"""review_rb_judge.sh calls the helper with inline env overrides
	(PR_CHECKS_REPOSITORY=... PR_CHECKS_SELF_RUN_ID=... _pr_checks_completed).
	The side-channel reason must still be readable afterward in the same shell."""
	res = _run(
		'PR_CHECKS_REPOSITORY="owner/repo" PR_CHECKS_SELF_RUN_ID="999" '
		'_pr_checks_completed 5 "abc1234" "main" >/dev/null 2>&1; '
		'printf "%s\\n" "${PR_CHECKS_LAST_REASON}"',
		runs_json=_page([{"name": "CI", "status": "completed", "conclusion": "failure"}]),
	)
	assert res.returncode == 0, res.stderr
	assert res.stdout.strip() == "blocking", res.stdout


# ---------------------------------------------------------------------------
# Required-names resolution helper.
# ---------------------------------------------------------------------------


def test_required_names_uses_env_default_when_unset() -> None:
	res = _run(
		'unset ORCH_FINAL_MERGE_REQUIRED_CHECKS; '
		'_pr_required_check_names_for_base ""',
		env=REPO_ENV,
	)
	assert res.returncode == 0, res.stderr
	assert res.stdout.strip() == (
		"CI,Integration PR readiness check,"
		"Lint plan-archival completeness,"
		"Lint PR body for auto-close keywords against orchestrator-tracking issues,"
		"review / gate"
	)


def test_required_names_preserves_explicit_empty_allow_all() -> None:
	res = _run(
		'_pr_required_check_names_for_base ""',
		env={**REPO_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""},
	)
	assert res.returncode == 0, res.stderr
	assert res.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Drift guard: library default == orchestrator default.
# ---------------------------------------------------------------------------


def test_library_default_matches_orchestrator_default() -> None:
	"""scripts/pr_checks_lib.sh and scripts/orchestrate_poll_process.sh must
	declare the SAME ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT string. The
	poller keeps a redundant assignment as a fail-safe (so a sourcing
	failure can never reach the empty-string allow-all sentinel); this test
	pins the two literals equal so they cannot drift."""
	import re

	lib_text = LIB.read_text(encoding="utf-8")
	poller_text = POLLER.read_text(encoding="utf-8")
	lib_m = re.search(
		r'ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT:=([^}]*)\}', lib_text
	)
	poller_m = re.search(
		r'ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT="([^"]*)"', poller_text
	)
	assert lib_m, "pr_checks_lib.sh must declare ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT via :="
	assert poller_m, "orchestrate_poll_process.sh must keep its DEFAULT assignment as a fail-safe"
	assert lib_m.group(1) == poller_m.group(1), (
		"ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT drifted between "
		"pr_checks_lib.sh and orchestrate_poll_process.sh:\n"
		f"  lib   : {lib_m.group(1)!r}\n"
		f"  poller: {poller_m.group(1)!r}"
	)


def test_orchestrator_rb_gates_pass_base_ref() -> None:
	"""All four orchestrator review-blocked merge gates must call
	_pr_checks_completed with a 3rd (base-ref) argument so they use the
	required-checks filter — not the legacy block-on-any-failure 2-arg
	form that deadlocks on a permanently-red non-required check."""
	import re

	poller_text = POLLER.read_text(encoding="utf-8")
	# 2-arg RB gate calls (PR + sha, no base ref) must NOT exist anymore.
	two_arg = re.findall(
		r'_pr_checks_completed "\$\{RB_PR\}" "\$\{_rb_[a-z]+_sha\}"(?!\s+")',
		poller_text,
	)
	assert not two_arg, (
		"orchestrate_poll_process.sh still has 2-arg review-blocked "
		f"_pr_checks_completed calls (legacy all-checks mode): {two_arg}. "
		"Every RB gate must pass the base ref so the required-checks filter "
		"applies."
	)
	# Exactly the four RB gates pass a base ref.
	three_arg = re.findall(
		r'_pr_checks_completed "\$\{RB_PR\}" "\$\{_rb_[a-z]+_sha\}" "\$\{_rb_[a-z]+_base\}"',
		poller_text,
	)
	assert len(three_arg) == 4, (
		"expected exactly 4 review-blocked merge gates passing a base ref "
		f"(merge / force-merge / no-fix / merge_with_followup); found "
		f"{len(three_arg)}: {three_arg}"
	)


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = skipped = failed = 0
	for func in test_funcs:
		try:
			func()
			print(f"  PASS  {func.__name__}")
			passed += 1
		except unittest.SkipTest as exc:
			print(f"  SKIP  {func.__name__}: {exc}")
			skipped += 1
		except Exception as exc:  # noqa: BLE001
			print(f"  FAIL  {func.__name__}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
