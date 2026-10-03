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


def _guard_rc(issue_num: str, pr_json: str) -> int:
	"""Source the extracted helpers and return the guard's exit code."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
		bootstrap = textwrap.dedent(f"""
		set -euo pipefail
		{extractor}
		: > helpers.sh
		extract_fn '_pr_json_closes_issue' >> helpers.sh
		extract_fn '_pr_json_is_issue_implementation_pr' >> helpers.sh
		""")
		r = _run_bash(bootstrap, cwd=tmp)
		assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
		body = (tmp / "helpers.sh").read_text(encoding="utf-8")
		assert "_pr_json_closes_issue()" in body, "missing _pr_json_closes_issue in helpers.sh"
		assert "_pr_json_is_issue_implementation_pr()" in body, "missing guard in helpers.sh"
		pr_json_file = tmp / "pr.json"
		pr_json_file.write_text(pr_json, encoding="utf-8")
		script = textwrap.dedent(f"""
		set -uo pipefail
		source helpers.sh
		_pr_json_is_issue_implementation_pr '{issue_num}' "$(cat pr.json)"
		""")
		r = _run_bash(script, cwd=tmp)
		return r.returncode


def _pr(head_ref: str, body: str) -> str:
	return json.dumps({
		"number": 3825,
		"state": "closed",
		"merged_at": "2026-08-25T15:18:27Z",
		"body": body,
		"head": {"ref": head_ref, "sha": "0" * 40},
		"base": {"ref": "main"},
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
# _pr_json_merged_into_issue_target — the target-branch and identity rule
# every poller path applies before it marks an issue merged (issue #5618:
# an unrelated PR saying "Fixes #<n>" merged into the wrong branch made the
# reconcile loop force ai:merged and mark the wave child merged, although
# close_merged_issues_sweep already rejects that merge since #4813 / #5226).
# ---------------------------------------------------------------------------

GH_HELPERS_SCRIPT = REPO_ROOT / "scripts" / "gh_helpers.sh"
_TEST_REPO = "shubhodeep1/coding-workflows"
_PROJECT_BRANCH = "orchestrator/project-5600"

_EXTRACT_GH_HELPER_FN = r"""
extract_gh_helper_fn() {
	local fn="$1"
	awk -v fn="${fn}" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__GH_HELPERS__"
}
"""


def _bootstrap_target_helpers(tmp: Path, poller_fns: list[str], gh_helper_fns: list[str]) -> None:
	extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
	gh_extractor = _EXTRACT_GH_HELPER_FN.replace("__GH_HELPERS__", str(GH_HELPERS_SCRIPT))
	lines = ["set -euo pipefail", extractor, gh_extractor, ": > helpers.sh"]
	lines += [f"extract_fn '{fn}' >> helpers.sh" for fn in poller_fns]
	lines += [f"extract_gh_helper_fn '{fn}' >> helpers.sh" for fn in gh_helper_fns]
	r = _run_bash("\n".join(lines), cwd=tmp)
	assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
	body = (tmp / "helpers.sh").read_text(encoding="utf-8")
	for fn in poller_fns + gh_helper_fns:
		assert f"{fn}()" in body, f"missing {fn} in helpers.sh"


def _target_pr(
	issue: int,
	*,
	base: str,
	head: str,
	head_repo: str = _TEST_REPO,
	default_branch: str = "main",
	base_repo: str = _TEST_REPO,
) -> str:
	return json.dumps({
		"number": 9001,
		"state": "closed",
		"merged_at": "2026-09-30T10:00:00Z",
		"body": f"Fixes #{issue}",
		"head": {"ref": head, "sha": "0" * 40, "repo": {"full_name": head_repo}},
		"base": {"ref": base, "repo": {"full_name": base_repo, "default_branch": default_branch}},
	})


def _target_rc(
	issue: str,
	pr_json: str,
	*targets: str,
	with_identity_helper: bool = True,
) -> tuple[int, str]:
	"""Return (rc, ISSUE_TARGET_MERGE_REJECT_REASON) of the extracted predicate."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap_target_helpers(
			tmp,
			["_pr_json_merged_into_issue_target"],
			["pr_head_ref_is_issue_automation_branch"] if with_identity_helper else [],
		)
		(tmp / "pr.json").write_text(pr_json, encoding="utf-8")
		quoted_targets = " ".join(f"'{t}'" for t in targets)
		script = textwrap.dedent(f"""
		set -uo pipefail
		export GITHUB_REPOSITORY='{_TEST_REPO}'
		source helpers.sh
		_rc=0
		_pr_json_merged_into_issue_target '{issue}' "$(cat pr.json)" {quoted_targets} || _rc=$?
		printf 'RC=%s\\n' "${{_rc}}"
		printf 'REASON=%s\\n' "${{ISSUE_TARGET_MERGE_REJECT_REASON}}"
		""")
		r = _run_bash(script, cwd=tmp)
		assert r.returncode == 0, f"predicate harness exited {r.returncode}: {r.stderr}"
		rc = -1
		reason = ""
		for line in r.stdout.splitlines():
			if line.startswith("RC="):
				rc = int(line[len("RC="):])
			if line.startswith("REASON="):
				reason = line[len("REASON="):]
		return rc, reason


def test_target_default_branch_merge_is_accepted():
	"""A default-branch merge keeps the closing-keyword identity, as the sweep does."""
	pr = _target_pr(5618, base="main", head="claude/any-branch", head_repo="someone/fork")
	assert _target_rc("5618", pr, _PROJECT_BRANCH) == (0, "")


def test_target_project_branch_merge_from_automation_head_is_accepted():
	pr = _target_pr(5618, base=_PROJECT_BRANCH, head="ai/issue-5618")
	assert _target_rc("5618", pr, _PROJECT_BRANCH) == (0, "")
	followup = _target_pr(5618, base=_PROJECT_BRANCH, head="fix/5618-followup-1790000000")
	assert _target_rc("5618", followup, _PROJECT_BRANCH) == (0, "")


def test_target_wrong_base_merge_is_rejected():
	"""The finding: `Fixes #N` merged into an unrelated branch never counts."""
	pr = _target_pr(5618, base="feature/unrelated", head="ai/issue-5618")
	assert _target_rc("5618", pr, _PROJECT_BRANCH) == (1, "non_target_base")


def test_target_branch_merge_from_foreign_head_is_rejected():
	pr = _target_pr(5618, base=_PROJECT_BRANCH, head="claude/unrelated-fix")
	assert _target_rc("5618", pr, _PROJECT_BRANCH) == (1, "unverified_identity")
	other_issue = _target_pr(5618, base=_PROJECT_BRANCH, head="ai/issue-5617")
	assert _target_rc("5618", other_issue, _PROJECT_BRANCH) == (1, "unverified_identity")


def test_target_branch_merge_from_fork_head_is_rejected():
	pr = _target_pr(5618, base=_PROJECT_BRANCH, head="ai/issue-5618", head_repo="attacker/coding-workflows")
	assert _target_rc("5618", pr, _PROJECT_BRANCH) == (1, "unverified_identity")
	no_repo = _target_pr(5618, base=_PROJECT_BRANCH, head="ai/issue-5618", head_repo="")
	assert _target_rc("5618", no_repo, _PROJECT_BRANCH) == (1, "unverified_identity")


def test_target_unknowns_fail_closed():
	"""Empty default branch, empty targets, empty base, and bad input are all rejections."""
	no_default = _target_pr(5618, base="main", head="ai/issue-5618", default_branch="")
	assert _target_rc("5618", no_default, _PROJECT_BRANCH)[0] == 1
	on_project = _target_pr(5618, base=_PROJECT_BRANCH, head="ai/issue-5618")
	assert _target_rc("5618", on_project) == (1, "non_target_base")
	assert _target_rc("5618", on_project, "") == (1, "non_target_base")
	no_base = _target_pr(5618, base="", head="ai/issue-5618")
	assert _target_rc("5618", no_base, "") == (1, "missing_base")
	assert _target_rc("5618", "{}", _PROJECT_BRANCH) == (1, "invalid_input")
	assert _target_rc("not-a-number", on_project, _PROJECT_BRANCH) == (1, "invalid_input")


def test_target_foreign_base_repository_is_rejected():
	"""A merge into another repository's same-named branch never counts (PR #5646 review round 1)."""
	on_default = _target_pr(5618, base="main", head="ai/issue-5618", base_repo="attacker/other-repo")
	assert _target_rc("5618", on_default, _PROJECT_BRANCH) == (1, "foreign_base_repo")
	on_project = _target_pr(5618, base=_PROJECT_BRANCH, head="ai/issue-5618", base_repo="attacker/other-repo")
	assert _target_rc("5618", on_project, _PROJECT_BRANCH) == (1, "foreign_base_repo")
	no_base_repo = _target_pr(5618, base="main", head="ai/issue-5618", base_repo="")
	assert _target_rc("5618", no_base_repo, _PROJECT_BRANCH) == (1, "foreign_base_repo")


def test_target_repository_slugs_compare_case_insensitively():
	"""GitHub resolves repository slugs case-insensitively, so a different
	casing of the same repository is not foreign (PR #5633 review round 4);
	another repository is still rejected in any casing."""
	upper = _TEST_REPO.upper()
	on_project = _target_pr(5618, base=_PROJECT_BRANCH, head="ai/issue-5618", base_repo=upper, head_repo=upper)
	assert _target_rc("5618", on_project, _PROJECT_BRANCH) == (0, "")
	on_default = _target_pr(5618, base="main", head="claude/any-branch", base_repo=upper)
	assert _target_rc("5618", on_default, _PROJECT_BRANCH) == (0, "")
	foreign = _target_pr(5618, base=_PROJECT_BRANCH, head="ai/issue-5618", base_repo="ATTACKER/OTHER-REPO")
	assert _target_rc("5618", foreign, _PROJECT_BRANCH) == (1, "foreign_base_repo")
	fork_head = _target_pr(5618, base=_PROJECT_BRANCH, head="ai/issue-5618", head_repo="Attacker/Coding-Workflows")
	assert _target_rc("5618", fork_head, _PROJECT_BRANCH) == (1, "unverified_identity")


def test_target_missing_identity_helper_fails_closed():
	pr = _target_pr(5618, base=_PROJECT_BRANCH, head="ai/issue-5618")
	assert _target_rc("5618", pr, _PROJECT_BRANCH, with_identity_helper=False) == (1, "unverified_identity")


def test_target_any_listed_target_counts():
	"""Stall recovery passes the body's integration branch and the managed project branch."""
	pr = _target_pr(5618, base="claude/implement-plan-issue-4813-x", head="ai/issue-5618")
	assert _target_rc("5618", pr, "", "claude/implement-plan-issue-4813-x")[0] == 0
	assert _target_rc("5618", pr, "claude/implement-plan-issue-4813-x", "")[0] == 0


# ---------------------------------------------------------------------------
# _reconcile_merged_pr_issue — stall recovery's ai:merged tag (issue #5618).
# gh, gh_retry, _safe_gh_jq and _fetch_pr_json are stubbed; the label edit is
# recorded to a file so the tests see whether the tag was written.
# ---------------------------------------------------------------------------


def _reconcile_run(
	pr_payload: dict | None,
	issue_payload: dict | None,
	*,
	known_issue_payload: dict | None = None,
	known_pr_payload: dict | None = None,
) -> tuple[list[str], str, str, int]:
	"""Run _reconcile_merged_pr_issue for issue 5618 / PR 9001.

	``known_issue_payload`` is passed as the optional fifth argument (the
	issue JSON the managed caller already read). Returns (gh calls, stderr,
	healing notes, return code). Return code 0 means the caller skips the
	stall action, 1 means it runs it (PR #5633, AD-8). Reads of the issue
	through _safe_gh_jq are logged to gh_calls.log as ``issue-read``."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap_target_helpers(
			tmp,
			[
				"_jq_field",
				"_pr_json_closes_issue",
				"_pr_json_is_issue_implementation_pr",
				"_pr_json_merged_into_issue_target",
				"_reconcile_merged_pr_issue",
			],
			[
				"pr_head_ref_is_issue_automation_branch",
				"issue_body_integration_branch",
				"issue_body_orchestrator_project_branch",
			],
		)
		(tmp / "pr.json").write_text(json.dumps(pr_payload) if pr_payload is not None else "", encoding="utf-8")
		(tmp / "issue.json").write_text(json.dumps(issue_payload) if issue_payload is not None else "", encoding="utf-8")
		script = textwrap.dedent(f"""
		set -euo pipefail
		export GITHUB_REPOSITORY='{_TEST_REPO}'
		gh() {{ printf '%s\\n' "$*" >> gh_calls.log; }}
		gh_retry() {{ "$@"; }}
		_fetch_pr_json() {{ printf 'pr-read\\n' >> gh_calls.log; if [ -s pr.json ]; then cat pr.json; else echo '{{}}'; fi; }}
		_safe_gh_jq() {{ printf 'issue-read\\n' >> gh_calls.log; if [ -s issue.json ]; then cat issue.json; else return 1; fi; }}
		add_healing_note() {{ printf '%s\\n' "$*" >> healing.log; }}
		tg_notify() {{ :; }}
		_gh_url() {{ printf 'https://github.com/%s/%s' "${{GITHUB_REPOSITORY}}" "$1"; }}
		source helpers.sh
		: > gh_calls.log
		: > healing.log
		known_issue_json="$(cat known_issue.json)"
		known_pr_json="$(cat known_pr.json)"
		if _reconcile_merged_pr_issue '5618' 'ai:done' 'retrigger_review' '9001' "${{known_issue_json}}" "${{known_pr_json}}"; then
			echo 0 > rc.txt
		else
			echo $? > rc.txt
		fi
		""")
		(tmp / "known_issue.json").write_text(
			json.dumps(known_issue_payload) if known_issue_payload is not None else "", encoding="utf-8"
		)
		(tmp / "known_pr.json").write_text(
			json.dumps(known_pr_payload) if known_pr_payload is not None else "", encoding="utf-8"
		)
		r = _run_bash(script, cwd=tmp)
		assert r.returncode == 0, f"reconcile harness exited {r.returncode}: {r.stderr}"
		calls = [c for c in (tmp / "gh_calls.log").read_text(encoding="utf-8").splitlines() if c]
		notes = (tmp / "healing.log").read_text(encoding="utf-8")
		rc = int((tmp / "rc.txt").read_text(encoding="utf-8").strip())
		return calls, r.stderr, notes, rc


def _merged_payload(base: str, head: str, body: str = "Fixes #5618", head_repo: str = _TEST_REPO) -> dict:
	return json.loads(_target_pr(5618, base=base, head=head, head_repo=head_repo)) | {"body": body}


def _issue_payload(body: str, labels: list[str] | None = None) -> dict:
	return {"number": 5618, "body": body, "labels": [{"name": n} for n in (labels or [])]}


def _labelled(calls: list[str]) -> bool:
	return any("issue edit 5618" in c and "--add-label ai:merged" in c for c in calls)


def test_reconcile_default_branch_merge_is_tagged():
	calls, _, notes, rc = _reconcile_run(_merged_payload("main", "claude/fix"), _issue_payload("Plain issue."))
	assert _labelled(calls), calls
	assert "tagged ai:merged" in notes
	assert rc == 0, "a verified merge skips the stall action"


def test_reconcile_reuses_the_pr_json_the_caller_already_read():
	"""PR #5633 review round 13 (CLAUDE.md §15): the REST fallbacks already fetched `pulls/<n>`; passed as the sixth
	argument it is used instead of a second read, but only when its number is the PR being judged."""
	pr = _merged_payload("main", "claude/fix") | {"number": 9001}
	calls, _, _, rc = _reconcile_run(pr, _issue_payload("Plain issue."), known_pr_payload=pr)
	assert _labelled(calls), calls
	assert "pr-read" not in calls, calls
	assert rc == 0
	other = pr | {"number": 9002}
	calls, _, _, _ = _reconcile_run(pr, _issue_payload("Plain issue."), known_pr_payload=other)
	assert calls.count("pr-read") == 1, calls

def test_reconcile_integration_branch_merge_from_automation_head_is_tagged():
	issue = _issue_payload("- Integration branch: `claude/implement-plan-issue-4813-x`\n")
	calls, _, _, rc = _reconcile_run(_merged_payload("claude/implement-plan-issue-4813-x", "ai/issue-5618"), issue)
	assert _labelled(calls), calls
	assert rc == 0


def test_reconcile_managed_child_project_branch_merge_is_tagged():
	issue = _issue_payload("Tracking issue: #5600\n", ["ai:orchestrator-managed"])
	calls, _, _, rc = _reconcile_run(_merged_payload(_PROJECT_BRANCH, "ai/issue-5618"), issue)
	assert _labelled(calls), calls
	assert rc == 0


def test_reconcile_unlabelled_project_branch_merge_is_not_tagged():
	"""Body text alone never makes an issue managed (issue #4957)."""
	issue = _issue_payload("Tracking issue: #5600\nManaged by: AI Orchestrator\n")
	calls, err, notes, rc = _reconcile_run(_merged_payload(_PROJECT_BRANCH, "ai/issue-5618"), issue)
	assert not _labelled(calls), calls
	assert "STALL_MERGED_LABEL_REJECTED issue=5618 pr=9001" in err
	assert "reason=non_target_base stall_action=run" in err
	assert "running stall recovery" in notes and "not tagged ai:merged" in notes
	assert rc == 1, "a rejected merge must not skip stall recovery (PR #5633, AD-8)"


def test_reconcile_wrong_base_merge_is_not_tagged():
	"""The finding's scenario through the stall path: the merge is not this
	issue's, so the caller runs stall recovery instead of skipping it."""
	issue = _issue_payload("- Integration branch: `claude/implement-plan-issue-4813-x`\n")
	calls, err, _, rc = _reconcile_run(_merged_payload("feature/unrelated", "claude/unrelated"), issue)
	assert not _labelled(calls), calls
	assert "reason=non_target_base stall_action=run" in err
	assert rc == 1


def test_reconcile_foreign_head_into_integration_branch_is_not_tagged():
	issue = _issue_payload("- Integration branch: `claude/implement-plan-issue-4813-x`\n")
	calls, err, _, rc = _reconcile_run(_merged_payload("claude/implement-plan-issue-4813-x", "claude/unrelated"), issue)
	assert not _labelled(calls), calls
	assert "reason=unverified_identity stall_action=run" in err
	assert rc == 1


def test_reconcile_mention_only_or_unmerged_pr_is_not_tagged():
	calls, err, _, rc = _reconcile_run(_merged_payload("main", "claude/fix", body="Refs #5618"), _issue_payload(""))
	assert not _labelled(calls), calls
	assert "reason=not_implementation_pr stall_action=run" in err
	assert rc == 1
	unmerged = _merged_payload("main", "ai/issue-5618") | {"merged_at": None}
	calls, err, _, rc = _reconcile_run(unmerged, _issue_payload(""))
	assert not _labelled(calls), calls
	assert "reason=not_merged stall_action=run" in err
	assert rc == 1


def test_reconcile_fetch_failures_fail_closed():
	"""A failed read verifies nothing: no label, and the stall action is
	skipped this cycle so the next cycle retries (return 0)."""
	calls, err, notes, rc = _reconcile_run(None, _issue_payload(""))
	assert not _labelled(calls), calls
	assert "reason=pr_fetch_failed stall_action=skip" in err
	assert "retrying next cycle" in notes
	assert rc == 0
	calls, err, _, rc = _reconcile_run(_merged_payload(_PROJECT_BRANCH, "ai/issue-5618"), None)
	assert not _labelled(calls), calls
	assert "reason=issue_fetch_failed stall_action=skip" in err
	assert rc == 0


def test_reconcile_unjudgeable_payload_skips_instead_of_running_recovery():
	"""PR #5633 round 3: a merged PR payload without a base ref is rejected as
	`missing_base`, which proves nothing, so the stall action is skipped this
	cycle (return 0) rather than run."""
	no_base = _merged_payload("main", "ai/issue-5618")
	no_base["base"] = {"repo": no_base["base"]["repo"]}
	calls, err, _, rc = _reconcile_run(no_base, _issue_payload(""))
	assert not _labelled(calls), calls
	assert "reason=missing_base stall_action=skip" in err
	assert rc == 0


def test_reconcile_reuses_known_issue_json_without_reading_the_issue():
	"""The managed path hands over the issue JSON it already read for its
	closed-issue guard; the reconcile then makes no second issue read
	(PR #5633 review round 1, finding 5)."""
	issue = _issue_payload("Tracking issue: #5600\n", ["ai:orchestrator-managed"])
	calls, _, _, rc = _reconcile_run(
		_merged_payload(_PROJECT_BRANCH, "ai/issue-5618"),
		None,
		known_issue_payload=issue,
	)
	assert "issue-read" not in calls, calls
	assert _labelled(calls), calls
	assert rc == 0


def test_reconcile_reads_issue_when_no_known_issue_json_is_passed():
	issue = _issue_payload("Plain issue.")
	calls, _, _, rc = _reconcile_run(_merged_payload("main", "claude/fix"), issue, known_issue_payload=None)
	assert "issue-read" in calls, calls
	assert rc == 0


def test_rejected_merged_link_counts_attempt_only_for_pr_acting_rungs():
	"""Q7: A on PR #5633: in ai:done / ai:ready-to-merge a rejected merged link
	counts the attempt instead of running a rung that acts on the linked PR;
	exhaustion (`skip`) and `escalate_human` still run, and every earlier
	phase runs its normal recovery."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap_target_helpers(tmp, ["_stall_rejected_merged_link_counts_attempt"], [])
		cases = [
			("ai:done", "retrigger_review", 0),
			("ai:done", "run_stall_judge", 0),
			("ai:ready-to-merge", "attempt_merge", 0),
			("ai:done", "skip", 1),
			("ai:done", "escalate_human", 1),
			("ai:ready-to-merge", "escalate_human", 1),
			("ai:planning", "retrigger_plan", 1),
			("no_labels", "retrigger_pipeline", 1),
			("ai:implementing", "retrigger_implement", 1),
		]
		lines = ["set -euo pipefail", "source helpers.sh"]
		for phase, action, _ in cases:
			lines.append(
				f"if _stall_rejected_merged_link_counts_attempt '{phase}' '{action}'; then echo '{phase} {action} 0'; else echo '{phase} {action} 1'; fi"
			)
		r = _run_bash("\n".join(lines), cwd=tmp)
		assert r.returncode == 0, r.stderr
		got = r.stdout.strip().splitlines()
		assert got == [f"{p} {a} {rc}" for p, a, rc in cases], got


def test_wave_gate_downgrade_failure_fails_the_gate_instead_of_wiping_states():
	"""PR #5633 review round 1, finding 4: a failed downgrade write returns 1
	(the caller defers the validate dispatch) rather than replacing every
	issue's linked-PR state with `{}`."""
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	gate = script.split("refresh_validation_dispatch_wave_gate() {", 1)[1].split("\n}\n", 1)[0]
	assert "'. + {($key): {state: \"unknown\", merged: false}}' 2>/dev/null || echo '{}'" not in gate
	assert "wave-merge gate unavailable this cycle" in gate
	downgrade_block = gate.split("could not downgrade the rejected linked PR", 1)[1]
	assert downgrade_block.lstrip().split("\n", 2)[1].strip() == "return 1", downgrade_block[:200]


# ---------------------------------------------------------------------------
# validation_fix_issue_has_merged_pr_evidence — the validation fix-up loop's
# ai:merged backfill. With the `issue_target` rule the merged PR must pass
# _pr_json_merged_into_issue_target (owner decision on PR #5633, AD-7): a
# `Fixes #<n>` PR merged into an unrelated branch is not evidence. Without a
# rule, an expected base keeps its exact-match meaning (security-pass
# callers). _issue_timeline_with_cross_refs_json and _fetch_pr_json are
# stubbed.
# ---------------------------------------------------------------------------


def _validation_evidence_rc(pr_payload: str, *args: str) -> tuple[int, str]:
	"""Return (rc, stderr) of validation_fix_issue_has_merged_pr_evidence for
	fix issue 5618 whose timeline holds merged PR 9001."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap_target_helpers(
			tmp,
			[
				"_pr_json_closes_issue",
				"_pr_json_is_issue_implementation_pr",
				"_pr_json_merged_into_issue_target",
				"validation_fix_issue_has_merged_pr_evidence",
			],
			["pr_head_ref_is_issue_automation_branch"],
		)
		timeline = [{
			"event": "cross-referenced",
			"source": {"issue": {"number": 9001, "merged": True, "pull_request": {"url": "https://example.invalid/pulls/9001"}}},
		}]
		(tmp / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
		(tmp / "pr.json").write_text(pr_payload, encoding="utf-8")
		quoted = " ".join(f"'{a}'" for a in args)
		script = textwrap.dedent(f"""
		set -euo pipefail
		export GITHUB_REPOSITORY='{_TEST_REPO}'
		_issue_timeline_with_cross_refs_json() {{ cat timeline.json; }}
		_fetch_pr_json() {{ cat pr.json; }}
		source helpers.sh
		if validation_fix_issue_has_merged_pr_evidence '5618' {quoted}; then
			echo 0 > rc.txt
		else
			echo $? > rc.txt
		fi
		""")
		r = _run_bash(script, cwd=tmp)
		assert r.returncode == 0, f"validation evidence harness exited {r.returncode}: {r.stderr}"
		return int((tmp / "rc.txt").read_text(encoding="utf-8").strip()), r.stderr


def test_validation_evidence_issue_target_rejects_wrong_base_merge():
	pr = _target_pr(5618, base="feature/unrelated", head="claude/unrelated")
	rc, err = _validation_evidence_rc(pr, "orchestrator/project-5600", "issue_target")
	assert rc == 1, err
	assert "VALIDATION_FIX_MERGED_EVIDENCE issue=5618 candidate_pr=9001 rejected=non_target_base" in err


def test_validation_evidence_issue_target_rejects_foreign_head_into_project_branch():
	pr = _target_pr(5618, base="orchestrator/project-5600", head="claude/unrelated")
	rc, err = _validation_evidence_rc(pr, "orchestrator/project-5600", "issue_target")
	assert rc == 1, err
	assert "rejected=unverified_identity" in err


def test_validation_evidence_issue_target_accepts_automation_head_into_project_branch():
	pr = _target_pr(5618, base="orchestrator/project-5600", head="ai/issue-5618")
	rc, err = _validation_evidence_rc(pr, "orchestrator/project-5600", "issue_target")
	assert rc == 0, err


def test_validation_evidence_issue_target_accepts_default_branch_merge():
	"""With no integration branch the project's target is the default branch."""
	pr = _target_pr(5618, base="main", head="ai/issue-5618")
	rc, err = _validation_evidence_rc(pr, "", "issue_target")
	assert rc == 0, err
	rc, err = _validation_evidence_rc(_target_pr(5618, base="release/x", head="ai/issue-5618"), "", "issue_target")
	assert rc == 1, err
	assert "rejected=non_target_base" in err


def test_validation_evidence_without_rule_keeps_exact_base_match():
	"""The security-pass callers pass an expected base and no rule: the base
	must equal it exactly, and a default-branch merge does not count."""
	rc, err = _validation_evidence_rc(_target_pr(5618, base="main", head="ai/issue-5618"), "orchestrator/project-5600")
	assert rc == 1, err
	assert "rejected=base_mismatch" in err
	rc, err = _validation_evidence_rc(_target_pr(5618, base="orchestrator/project-5600", head="claude/any"), "orchestrator/project-5600")
	assert rc == 0, err


# ---------------------------------------------------------------------------
# refresh_validation_dispatch_wave_gate — runtime, through the real
# check-wave-status. Its linked PR comes from the GraphQL batch, which
# carries cross-repository closing PRs too: a PR in another repository that
# says `Fixes <this repo>#<n>` and merged into that repository's default
# branch must not mark the child merged here (review round 1 on PR #5646).
# _fetch_candidate_issue_details_graphql, gh_retry, _safe_gh_jq and
# _integration_branch_ahead_of_default are stubbed.
# ---------------------------------------------------------------------------


def _wave_gate_linked_pr(
	*,
	base_ref: str,
	head_ref: str,
	base_repo: str | None = _TEST_REPO,
	head_repo: str | None = _TEST_REPO,
	default_branch: str | None = "main",
) -> dict:
	return {
		"number": 9001,
		"state": "MERGED",
		"merged": True,
		"merged_at": "2026-09-30T10:00:00Z",
		"head_ref": head_ref,
		"base_ref": base_ref,
		"head_repo": head_repo,
		"base_repo": base_repo,
		"default_branch": default_branch,
	}


def _wave_gate_run(linked_pr: dict, integration_branch: str = "") -> tuple[str, str]:
	"""Run refresh_validation_dispatch_wave_gate for a one-issue wave (#5618).

	Returns (WAVE_COMPLETE, stderr)."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap_target_helpers(
			tmp,
			[
				"_jq_field",
				"_pr_json_merged_into_issue_target",
				"refresh_validation_dispatch_wave_gate",
			],
			["pr_head_ref_is_issue_automation_branch"],
		)
		state = {
			"current_wave": 1,
			"integration_branch": integration_branch,
			"waves": [{"wave": 1, "issues": [{"id": "issue-1", "github_issue": 5618, "status": "pending"}]}],
		}
		(tmp / "state.json").write_text(json.dumps(state), encoding="utf-8")
		details = {"5618": {"state": "open", "labels": [], "linked_pr": linked_pr}}
		(tmp / "details.json").write_text(json.dumps(details), encoding="utf-8")
		script = textwrap.dedent(f"""
		set -uo pipefail
		export GITHUB_REPOSITORY='{_TEST_REPO}'
		STATE_FILE='{tmp / "state.json"}'
		_fetch_candidate_issue_details_graphql() {{ cat '{tmp / "details.json"}'; }}
		gh_retry() {{ "$@"; }}
		_safe_gh_jq() {{ echo main; }}
		_integration_branch_ahead_of_default() {{ echo 0; }}
		source '{tmp / "helpers.sh"}'
		WAVE_COMPLETE=unset
		refresh_validation_dispatch_wave_gate || echo "GATE_RC=$?"
		printf 'WAVE_COMPLETE=%s\\n' "${{WAVE_COMPLETE}}"
		""")
		# check-wave-status is invoked as scripts/orchestrate_lib.py, relative
		# to the repository root.
		r = _run_bash(script, cwd=REPO_ROOT)
		assert r.returncode == 0, f"wave gate harness exited {r.returncode}: {r.stderr}"
		assert "GATE_RC=" not in r.stdout, f"wave gate failed: {r.stdout}\n{r.stderr}"
		wave_complete = ""
		for line in r.stdout.splitlines():
			if line.startswith("WAVE_COMPLETE="):
				wave_complete = line[len("WAVE_COMPLETE="):]
		return wave_complete, r.stderr


def test_wave_gate_same_repo_default_branch_merge_completes_wave():
	wave_complete, err = _wave_gate_run(_wave_gate_linked_pr(base_ref="main", head_ref="claude/any-branch"))
	assert wave_complete == "true", err
	assert "LINKED_PR_CROSS_REF_REJECTED" not in err


def test_wave_gate_project_branch_automation_merge_completes_wave():
	pr = _wave_gate_linked_pr(base_ref=_PROJECT_BRANCH, head_ref="ai/issue-5618")
	wave_complete, err = _wave_gate_run(pr, integration_branch=_PROJECT_BRANCH)
	assert wave_complete == "true", err


def test_wave_gate_rejects_cross_repository_default_branch_merge():
	"""The review finding: a foreign PR merged into its own `main` never counts here."""
	foreign = _wave_gate_linked_pr(
		base_ref="main",
		head_ref="ai/issue-5618",
		base_repo="attacker/other-repo",
		head_repo="attacker/other-repo",
	)
	wave_complete, err = _wave_gate_run(foreign)
	assert wave_complete == "false", err
	assert "LINKED_PR_CROSS_REF_REJECTED issue=5618 pr=9001 base=main" in err
	assert "base_repo=attacker/other-repo" in err
	assert "reason=foreign_base_repo" in err
	assert "path=validation_dispatch_gate" in err


def test_wave_gate_rejects_cross_repository_project_branch_merge():
	"""Same branch name as the project branch, but in another repository."""
	foreign = _wave_gate_linked_pr(
		base_ref=_PROJECT_BRANCH,
		head_ref="ai/issue-5618",
		base_repo="attacker/other-repo",
	)
	wave_complete, err = _wave_gate_run(foreign, integration_branch=_PROJECT_BRANCH)
	assert wave_complete == "false", err
	assert "reason=foreign_base_repo" in err


def test_wave_gate_missing_base_repository_fails_closed():
	wave_complete, err = _wave_gate_run(_wave_gate_linked_pr(base_ref="main", head_ref="ai/issue-5618", base_repo=None))
	assert wave_complete == "false", err
	assert "reason=foreign_base_repo" in err


# ---------------------------------------------------------------------------
# Call-site wiring: the reconcile loop, the validation-dispatch wave gate, and
# the backward-scan promotion must consult the predicate before recording
# merged state (issue #5618).
# ---------------------------------------------------------------------------


def _poller_text() -> str:
	return POLLER_SCRIPT.read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
	start = text.index(f"\n{name}() {{")
	end = text.index("\n}\n", start)
	return text[start:end]


def test_reconcile_loop_gates_merged_candidates_before_adopting_them():
	text = _poller_text()
	start = text.index('LINKED_PR_CANDIDATES="$(_issue_cross_ref_pr_numbers_unique "${inum}"')
	loop = text[start:text.index('PR_STATES_JSON="$(echo "${PR_STATES_JSON}"', start)]
	gate = loop.index('_pr_json_merged_into_issue_target "${inum}" "${_linked_pr_candidate_json}" "${CWS_INTEGRATION_BRANCH:-}"')
	adopt = loop.index('LINKED_PR_NUM="${_linked_pr_candidate}"')
	merged = loop.index('PR_MERGED="${_linked_pr_candidate_merged:-false}"')
	assert gate < adopt < merged
	assert "reason=${ISSUE_TARGET_MERGE_REJECT_REASON:-non_target_base}" in loop[gate:adopt]
	assert "continue" in loop[gate:adopt]


def test_validation_dispatch_gate_rechecks_merged_links():
	body = _function_body(_poller_text(), "refresh_validation_dispatch_wave_gate")
	gate = body.index('_pr_json_merged_into_issue_target "${_vdg_inum}" "${_vdg_pr_json}" "${integration_branch}"')
	downgrade = body.index('{($key): {state: "unknown", merged: false}}')
	wave = body.index("check-wave-status")
	assert gate < downgrade < wave
	assert "path=validation_dispatch_gate" in body


def test_candidate_details_linked_pr_carries_target_fields():
	body = _function_body(_poller_text(), "_fetch_candidate_issue_details_graphql")
	assert "headRepository { nameWithOwner }" in body
	assert "baseRepository { nameWithOwner defaultBranchRef { name } }" in body
	assert "head_repo: (.headRepository.nameWithOwner // null)" in body
	assert "base_repo: (.baseRepository.nameWithOwner // null)" in body
	assert "default_branch: (.baseRepository.defaultBranchRef.name // null)" in body


def test_backward_scan_gates_ready_to_merge_promotion():
	text = _poller_text()
	start = text.index('_pw_pr_target_reject=""')
	promote = text.index('promoting to ai:merged."', start)
	block = text[start:promote]
	assert '_pr_json_is_issue_implementation_pr "${pw_inum}" "${_pw_pr_json}"' in block
	assert "_pr_json_merged_into_issue_target \"${pw_inum}\" \"${_pw_pr_json}\"" in block
	assert ".integration_branch" in block
	assert 'elif [ "${PW_PR_MERGED}" = "true" ]; then' in block


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
