#!/usr/bin/env python3
"""Runtime tests for _pr_json_is_issue_implementation_pr.

Background (the issue #3817 / PR #3825 incident): the poller resolves an
issue's "linked PR" via ``_issue_cross_ref_pr_number_last``, which returns the
LAST PR that cross-references the issue — including PRs that merely mention it.
PR #3825 (an unrelated fix whose body said "Refs #3817", the §19-correct
non-closing reference) was merged, became issue #3817's most recent
cross-reference, and the poller adopted it as the issue's implementation PR:
``reconcile_managed_issue_labels`` forced ``ai:merged`` and
``close_merged_issues_sweep`` closed the issue with its scope never
implemented.

The fix gates merged-state adoption on ``_pr_json_is_issue_implementation_pr``:
a candidate PR counts as the issue's implementation PR only when its head
branch is the orchestrator convention ``ai/issue-<n>`` or its body carries a
GitHub closing-keyword reference to the issue (``_pr_json_closes_issue``).

Uses the same function-extraction-plus-source pattern as
``test_retrigger_inflight_direct_fallback.py``: pull the helper definitions out
of the production script with awk and exercise them directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	full_env = os.environ.copy()
	full_env.pop("BASH_ENV", None)
	full_env.pop("ENV", None)
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=full_env,
		capture_output=True,
		text=True,
	)


_EXTRACT_FN = r"""
extract_fn() {
	local fn="$1"
	awk -v fn="${fn}" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__POLLER__"
}
"""


_DEFAULT_BRANCH_STUB = r"""
_review_run_default_branch() {
	if [ -n "${STUB_DEFAULT_BRANCH:-}" ]; then
		printf '%s\n' "${STUB_DEFAULT_BRANCH}"
		return 0
	fi
	return 1
}
"""


def _guard_rc(issue_num: str, pr_json: str) -> int:
	"""Source the extracted helpers and return the guard's exit code."""
	return _guard_result(issue_num, pr_json)[0]


def _guard_result(issue_num: str, pr_json: str, env: dict[str, str] | None = None) -> tuple[int, str]:
	"""Source the extracted helpers and return (exit code, stderr).

	The memoized default-branch helper is stubbed: STUB_DEFAULT_BRANCH set
	prints it, unset or empty fails the read like an unreadable repository.
	"""
	run_env = {
		"GITHUB_REPOSITORY": "shubhodeep1/coding-workflows",
		"STUB_DEFAULT_BRANCH": "main",
	}
	run_env["ORCH_IMPLEMENTS_MARKER_ENABLED"] = "true"
	if env:
		run_env.update(env)
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
		bootstrap = textwrap.dedent(f"""
		set -euo pipefail
		{extractor}
		: > helpers.sh
		extract_fn '_pr_json_closes_issue' >> helpers.sh
		extract_fn '_pr_json_implements_issue' >> helpers.sh
		extract_fn '_pr_json_is_issue_implementation_pr' >> helpers.sh
		""")
		r = _run_bash(bootstrap, cwd=tmp)
		assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
		body = (tmp / "helpers.sh").read_text(encoding="utf-8")
		assert "_pr_json_closes_issue()" in body, "missing _pr_json_closes_issue in helpers.sh"
		assert "_pr_json_is_issue_implementation_pr()" in body, "missing guard in helpers.sh"
		assert "_pr_json_implements_issue()" in body, "missing marker helper in helpers.sh"
		(tmp / "helpers.sh").write_text(body + _DEFAULT_BRANCH_STUB, encoding="utf-8")
		pr_json_file = tmp / "pr.json"
		pr_json_file.write_text(pr_json, encoding="utf-8")
		script = textwrap.dedent(f"""
		set -uo pipefail
		source helpers.sh
		_pr_json_is_issue_implementation_pr '{issue_num}' "$(cat pr.json)"
		""")
		run_env["RUNTIME_DIR"] = str(tmp)
		r = _run_bash(script, cwd=tmp, env=run_env)
		return r.returncode, r.stderr


def _pr(head_ref: str, body: str, base_ref: str = "main") -> str:
	return json.dumps({
		"number": 3825,
		"state": "closed",
		"merged_at": "2026-08-25T15:18:27Z",
		"body": body,
		"head": {"ref": head_ref, "sha": "0" * 40},
		"base": {"ref": base_ref},
	})


def test_conventional_head_branch_is_accepted():
	"""ai/issue-<n> head branch is the orchestrator implement convention."""
	assert _guard_rc("3816", _pr("ai/issue-3816", "Automated implementation.")) == 0


def test_closing_hash_reference_is_accepted():
	"""A non-conventional branch with a closing-keyword #N body reference
	(e.g. a claude-branch or validation-fix PR) still qualifies."""
	assert _guard_rc("3817", _pr("claude/some-fix-branch", "Closes #3817")) == 0


def test_closing_url_reference_is_accepted():
	"""The implement pipeline writes URL-form closers ("Closes https://.../issues/N")."""
	body = "Automated implementation. Closes https://github.com/shubhodeep1/coding-workflows/issues/3816\nRefs #3810\n"
	assert _guard_rc("3816", _pr("claude/other-branch", body)) == 0


def test_refs_mention_only_pr_is_rejected():
	"""The incident case: a merged PR whose body only says "Refs #3817" is a
	mention, not the issue's implementation PR, and must be rejected."""
	body = "Raise default ai-memory push-retry budget.\n\nRefs #3817\n"
	assert _guard_rc("3817", _pr("claude/ai-planning-workflow-failure-fwufit", body)) == 1


def test_closing_reference_to_other_issue_is_rejected():
	"""A closer aimed at a different issue number must not qualify."""
	assert _guard_rc("3817", _pr("claude/branch", "Closes #3816")) == 1


def test_head_branch_for_other_issue_is_rejected():
	"""ai/issue-<other> is some other issue's implementation branch."""
	assert _guard_rc("3817", _pr("ai/issue-3816", "Automated implementation.")) == 1


def test_empty_or_invalid_pr_json_is_rejected():
	"""Adopting merged state is the destructive act, so unverifiable
	candidates map to rejection (unlike the fail-closed skip-guards)."""
	assert _guard_rc("3817", "") == 1
	assert _guard_rc("3817", "{}") == 1
	assert _guard_rc("not-a-number", _pr("ai/issue-3817", "Closes #3817")) == 1


# ---------------------------------------------------------------------------
# _resolve_issue_implementation_pr — the stall-recovery target resolver
# (issue #3816 / PR #3828 incident: retrigger_review pushed its empty commit
# onto an unrelated PR that merely said "Refs #3816", because the target came
# from _issue_cross_ref_pr_number_last).
#
# Dependencies (_linked_prs_by_branch_name, _issue_cross_ref_pr_numbers_unique,
# _fetch_pr_json) are stubbed via env-driven bash functions so the resolver's
# ordering and rejection logic is exercised in isolation.
# ---------------------------------------------------------------------------


def _resolver_result(
	branch_pr: str,
	cross_refs: str,
	pr_payloads: dict[str, dict],
	*,
	branch_discovery_rc: int = 0,
	cross_ref_discovery_rc: int = 0,
) -> tuple[int, str, str, str]:
	"""Run the extracted resolver with stubbed lookups.

	branch_pr: newline list emitted by the _linked_prs_by_branch_name stub.
	cross_refs: newline list emitted by the _issue_cross_ref_pr_numbers_unique stub.
	pr_payloads: PR number -> payload dict served by the _fetch_pr_json stub
	  (missing numbers yield "{}", the helper's fetch-failure shape).
	branch_discovery_rc / cross_ref_discovery_rc: injected helper return codes.
	Returns (rc, resolved_pr_num, failure_reason, stderr).
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
		bootstrap = textwrap.dedent(f"""
		set -euo pipefail
		{extractor}
		: > helpers.sh
		extract_fn '_pr_json_closes_issue' >> helpers.sh
		extract_fn '_pr_json_implements_issue' >> helpers.sh
		extract_fn '_pr_json_is_issue_implementation_pr' >> helpers.sh
		extract_fn '_resolve_issue_implementation_pr' >> helpers.sh
		""")
		r = _run_bash(bootstrap, cwd=tmp)
		assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
		body = (tmp / "helpers.sh").read_text(encoding="utf-8")
		assert "_resolve_issue_implementation_pr()" in body, "missing resolver in helpers.sh"
		(tmp / "payloads.json").write_text(json.dumps(pr_payloads), encoding="utf-8")
		script = textwrap.dedent("""
		set -uo pipefail
		_linked_prs_by_branch_name() { printf '%s\\n' "${STUB_BRANCH_PRS}"; return "${STUB_BRANCH_DISCOVERY_RC}"; }
		_issue_cross_ref_pr_numbers_unique() { printf '%s\\n' "${STUB_CROSS_REF_PRS}"; return "${STUB_CROSS_REF_DISCOVERY_RC}"; }
		_fetch_pr_json() { jq -c --arg n "$1" '.[$n] // {}' payloads.json; }
		source helpers.sh
		_resolver_rc=0
		_resolve_issue_implementation_pr '3816' || _resolver_rc=$?
		printf 'RC=%s\\n' "${_resolver_rc}"
		printf 'RESOLVED=%s\\n' "${STALL_IMPL_PR_NUM}"
		printf 'FAILURE_REASON=%s\\n' "${STALL_IMPL_PR_FAILURE_REASON}"
		""")
		r = _run_bash(script, cwd=tmp, env={
			"STUB_BRANCH_PRS": branch_pr,
			"STUB_CROSS_REF_PRS": cross_refs,
			"STUB_BRANCH_DISCOVERY_RC": str(branch_discovery_rc),
			"STUB_CROSS_REF_DISCOVERY_RC": str(cross_ref_discovery_rc),
		})
		assert r.returncode == 0, f"resolver harness exited {r.returncode}: {r.stderr}"
		resolved = ""
		failure_reason = ""
		rc = -1
		for line in r.stdout.splitlines():
			if line.startswith("RESOLVED="):
				resolved = line[len("RESOLVED="):]
			if line.startswith("FAILURE_REASON="):
				failure_reason = line[len("FAILURE_REASON="):]
			if line.startswith("RC="):
				rc = int(line[len("RC="):])
		return rc, resolved, failure_reason, r.stderr


def _impl_payload(num: int, head_ref: str, body: str) -> dict:
	return {
		"number": num,
		"state": "open",
		"merged_at": None,
		"body": body,
		"head": {"ref": head_ref, "sha": "0" * 40},
		"base": {"ref": "main"},
	}


def test_resolver_prefers_conventional_branch_pr():
	"""The open ai/issue-<n> PR wins even when a newer mention-only PR is the
	latest cross-reference — the exact incident shape."""
	rc, resolved, _, _ = _resolver_result(
		branch_pr="3823",
		cross_refs="3823\n3828",
		pr_payloads={
			"3823": _impl_payload(3823, "ai/issue-3816", "Automated implementation."),
			"3828": _impl_payload(3828, "claude/stall-recovery-pr-3823-3817-c6li3u", "Refs #3816"),
		},
	)
	assert rc == 0 and resolved == "3823", f"rc={rc} resolved={resolved}"


def test_resolver_falls_back_to_verified_cross_ref_and_skips_mentions():
	"""With no open conventional-branch PR, the cross-ref walk (newest first)
	must skip the mention-only PR and accept the closing-body one."""
	rc, resolved, failure_reason, err = _resolver_result(
		branch_pr="",
		cross_refs="3823\n3828",
		pr_payloads={
			"3823": _impl_payload(3823, "claude/other-branch", "Closes #3816"),
			"3828": _impl_payload(3828, "claude/unrelated", "Refs #3816"),
		},
	)
	assert rc == 0 and resolved == "3823", f"rc={rc} resolved={resolved}"
	assert "STALL_LINKED_PR_REJECTED issue=3816 pr=3828 reason=not_implementation_pr" in err


def test_resolver_returns_failure_when_only_mentions_exist():
	"""Nothing verifiable -> rc 1 and no target, so callers skip the
	destructive push instead of acting on a mention-only PR."""
	rc, resolved, failure_reason, err = _resolver_result(
		branch_pr="",
		cross_refs="3828",
		pr_payloads={
			"3828": _impl_payload(3828, "claude/unrelated", "Refs #3816"),
		},
	)
	assert rc == 1 and resolved == "", f"rc={rc} resolved={resolved}"
	assert failure_reason == "not_implementation_pr"
	assert "reason=not_implementation_pr" in err


def test_resolver_logs_fetch_failures_distinctly():
	"""A candidate whose pulls/<n> fetch fails is logged as pr_fetch_failed
	and never resolved."""
	rc, resolved, failure_reason, err = _resolver_result(
		branch_pr="",
		cross_refs="3999",
		pr_payloads={},
	)
	assert rc == 1 and resolved == "", f"rc={rc} resolved={resolved}"
	assert failure_reason == "pr_fetch_failed"
	assert "STALL_LINKED_PR_REJECTED issue=3816 pr=3999 reason=pr_fetch_failed" in err


def test_resolver_logs_conventional_branch_fetch_failures_distinctly():
	"""The preferred conventional-branch lookup reports the same diagnostic
	as a failed cross-reference lookup and stops resolution safely."""
	rc, resolved, failure_reason, err = _resolver_result(
		branch_pr="3998",
		cross_refs="",
		pr_payloads={},
	)
	assert rc == 1 and resolved == "", f"rc={rc} resolved={resolved}"
	assert failure_reason == "pr_fetch_failed"
	assert "STALL_LINKED_PR_REJECTED issue=3816 pr=3998 reason=pr_fetch_failed" in err


def test_resolver_preferred_fetch_failure_outweighs_mention_rejection():
	"""A failed payload fetch for the preferred conventional-branch PR stays
	inconclusive even when a newer mention-only cross-reference is rejectable."""
	rc, resolved, failure_reason, err = _resolver_result(
		branch_pr="3998",
		cross_refs="4000",
		pr_payloads={
			"4000": _impl_payload(4000, "claude/unrelated", "Refs #3816"),
		},
	)
	assert rc == 1 and resolved == "", f"rc={rc} resolved={resolved}"
	assert failure_reason == "pr_fetch_failed"
	assert "STALL_LINKED_PR_REJECTED issue=3816 pr=3998 reason=pr_fetch_failed" in err
	assert "pr=4000" not in err


def test_resolver_logs_conventional_branch_rejections_distinctly():
	"""A fetched conventional-branch candidate that fails the predicate is a
	genuine rejection, not a fetch failure."""
	rc, resolved, failure_reason, err = _resolver_result(
		branch_pr="3997",
		cross_refs="",
		pr_payloads={
			"3997": _impl_payload(3997, "claude/unrelated", "Refs #3816"),
		},
	)
	assert rc == 1 and resolved == "", f"rc={rc} resolved={resolved}"
	assert failure_reason == "not_implementation_pr"
	assert err.count("STALL_LINKED_PR_REJECTED issue=3816 pr=3997 reason=not_implementation_pr") == 1
	assert "reason=pr_fetch_failed" not in err


def test_resolver_branch_lookup_is_deterministic_and_pipefail_safe():
	"""Multiple conventional-branch matches choose the newest PR and stay fail-open."""
	rc, resolved, _, _ = _resolver_result(
		branch_pr="3997\n3998",
		cross_refs="",
		pr_payloads={
			"3997": _impl_payload(3997, "ai/issue-3816", "Automated implementation."),
			"3998": _impl_payload(3998, "ai/issue-3816", "Automated implementation."),
		},
	)
	assert rc == 0 and resolved == "3998", f"rc={rc} resolved={resolved}"


def test_resolver_successful_empty_discovery_is_conclusive():
	"""Successful discovery with no candidates is a verified absence."""
	rc, resolved, failure_reason, _ = _resolver_result("", "", {})
	assert rc == 1 and resolved == "", f"rc={rc} resolved={resolved}"
	assert failure_reason == "not_implementation_pr"


def test_resolver_discovery_failures_are_inconclusive():
	"""Either failed source can hide the real PR, even when the other source
	conclusively rejects a candidate."""
	cases = (
		("", "", {}, 1, 0),
		("", "3828", {"3828": _impl_payload(3828, "claude/unrelated", "Refs #3816")}, 1, 0),
		("3997", "", {"3997": _impl_payload(3997, "claude/unrelated", "Refs #3816")}, 0, 1),
	)
	for branch_pr, cross_refs, payloads, branch_rc, cross_ref_rc in cases:
		rc, resolved, failure_reason, _ = _resolver_result(
			branch_pr,
			cross_refs,
			payloads,
			branch_discovery_rc=branch_rc,
			cross_ref_discovery_rc=cross_ref_rc,
		)
		assert rc == 1 and resolved == "", f"rc={rc} resolved={resolved}"
		assert failure_reason == "pr_fetch_failed"


def test_resolver_rejection_outweighs_mixed_fetch_failures_in_either_order():
	"""One stale cross-reference must not make fetched rejections retry forever."""
	cases = (
		{"3998": _impl_payload(3998, "claude/unrelated", "Refs #3816")},
		{"4000": _impl_payload(4000, "claude/unrelated", "Refs #3816")},
	)
	for payloads in cases:
		rc, resolved, failure_reason, err = _resolver_result(
			branch_pr="",
			cross_refs="4000\n3998",
			pr_payloads=payloads,
		)
		assert rc == 1 and resolved == "", f"rc={rc} resolved={resolved}"
		assert failure_reason == "not_implementation_pr", failure_reason
		assert "reason=pr_fetch_failed" in err
		assert "reason=not_implementation_pr" in err


# ---------------------------------------------------------------------------
# Explicit "Implements #N" marker (fun-token-multi-chain#639): a person's PR
# that finishes a blocked orchestrator item links through this non-closing
# marker, while a bare "Refs #N" stays rejected (#3817).
# ---------------------------------------------------------------------------


def test_implements_marker_on_project_base_is_accepted():
	"""The fun-token-multi-chain#639 shape, with the marker added. No
	default-branch read is needed for an orchestrator/project-<n> base."""
	rc, err = _guard_result(
		"639",
		_pr("claude/amazing-galileo-e32yjo", "Implements #639", "orchestrator/project-579"),
		{"STUB_DEFAULT_BRANCH": ""},
	)
	assert rc == 0, err
	assert "LINKED_PR_IMPLEMENTS_MARKER issue=639 pr=3825 base=orchestrator/project-579 outcome=accepted" in err


def test_implements_marker_repo_and_url_forms_are_accepted():
	for body in (
		"implements shubhodeep1/coding-workflows#639",
		"Work done.\nImplements https://github.com/shubhodeep1/coding-workflows/issues/639\n",
	):
		rc, err = _guard_result("639", _pr("claude/x", body, "orchestrator/project-579"))
		assert rc == 0, (body, err)
		assert "outcome=accepted" in err


def test_implements_marker_on_default_branch_base_is_accepted():
	rc, err = _guard_result("639", _pr("claude/x", "Implements #639", "main"))
	assert rc == 0, err
	assert "base=main outcome=accepted" in err


def test_refs_without_marker_is_rejected_silently():
	rc, err = _guard_result("639", _pr("claude/x", "Refs #639", "orchestrator/project-579"))
	assert rc == 1
	assert "LINKED_PR_IMPLEMENTS_MARKER" not in err


def test_implements_marker_for_other_number_or_repo_is_rejected():
	for body in ("Implements #6390", "Implements other/repo#639", "Implements https://github.com/other/repo/issues/639"):
		rc, err = _guard_result("639", _pr("claude/x", body, "orchestrator/project-579"))
		assert rc == 1, (body, err)
		assert "LINKED_PR_IMPLEMENTS_MARKER" not in err, body


def test_implements_marker_on_feature_base_is_rejected():
	rc, err = _guard_result("639", _pr("claude/x", "Implements #639", "feature/x"))
	assert rc == 1
	assert "base=feature/x outcome=rejected reason=base_not_target" in err


def test_implements_marker_with_unreadable_default_branch_is_rejected():
	rc, err = _guard_result(
		"639",
		_pr("claude/x", "Implements #639", "feature/x"),
		{"STUB_DEFAULT_BRANCH": ""},
	)
	assert rc == 1
	assert "outcome=rejected reason=default_branch_unknown" in err


def test_conventional_head_and_closer_unchanged_with_marker_rules():
	assert _guard_result("639", _pr("ai/issue-639", "Automated implementation.", "feature/x"))[0] == 0
	assert _guard_result("639", _pr("claude/x", "Closes #639", "feature/x"))[0] == 0


def test_implements_marker_kill_switch():
	rc, err = _guard_result(
		"639",
		_pr("claude/x", "Implements #639", "orchestrator/project-579"),
		{"ORCH_IMPLEMENTS_MARKER_ENABLED": "false"},
	)
	assert rc == 1
	assert "LINKED_PR_IMPLEMENTS_MARKER" not in err


# ---------------------------------------------------------------------------
# Direct-invocation entrypoint
#
# `.github/workflows/*.yml` runs this test as `python3 tests/<file>.py` from
# explicit allowlists (no pytest discovery). Without this block, the file would
# import successfully and exit 0 without running any test_* functions.
# ---------------------------------------------------------------------------


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
