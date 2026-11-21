#!/usr/bin/env python3
"""Runtime tests for the close_and_reissue green-PR guard (issue #9892).

On 2026-11-21 stall recovery closed issue #9398 and its implementation PR
#9402 unmerged although #9402 was green (`ai-review/head-gate` and `lint`
passed) and only waiting for the merge lease (`ai:merge-lease-wait`). The
stall judge had been shown the unrelated, closed #9685 as "the linked PR".

Covers:
- ``_stall_reissue_green_pr_guard`` refuses a held (merge-lease wait,
  draft) or green open implementation PR, fails closed on read errors,
  ignores cross-reference-only PRs, binds `lint` to ci.yml runs, and honours
  its kill switch.
- An end-to-end replay of the #9398 state through ``invoke_stall_judge``
  and the ``close_and_reissue`` arm of ``execute_stall_recovery_action``:
  no PR close, no issue close, and a ``STALL_REISSUE_REFUSED`` line.

Same function-extraction-plus-stub pattern as
``tests/test_stall_recovery_pr_lookup.py``.
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

HEAD_SHA = "f4ed803b" + "0" * 32

_FUNCTIONS = (
	"_fetch_pr_json",
	"_jq_field",
	"_linked_prs_by_branch_name",
	"_linked_prs_by_body_reference",
	"_find_all_linked_prs",
	"_linked_pr_is_issue_implementation",
	"_stall_judge_pick_linked_pr",
	"_stall_reissue_guard_refuse",
	"_stall_reissue_green_pr_guard",
	"_stall_pr_ai_hold_reason",
	"_stall_merge_lease_label_age_minutes",
	"close_linked_pr",
	"invoke_stall_judge",
	"execute_stall_recovery_action",
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

_STUBS = r"""
gh_retry() { "$@"; }

# Serves canned GitHub API payloads by request path (query string included).
# API_FAIL[<path>]=1 makes that request fail like a real API error.
_safe_gh_jq() {
	local path="$1"
	printf '%s\n' "${path}" >> "${API_LOG:-/dev/null}"
	if [ -n "${API_FAIL[${path}]:-}" ]; then
		return 1
	fi
	if [ -z "${API_JSON[${path}]+set}" ]; then
		return 1
	fi
	printf '%s' "${API_JSON[${path}]}"
}

gh() {
	local sub="${1:-}" sub2="${2:-}"
	printf '%s\n' "$*" >> "${GH_LOG:-/dev/null}"
	case "${sub}-${sub2}" in
		pr-list)
			local use_search=0 use_head=0 jq_filter="" prev="" arg
			for arg in "$@"; do
				[ "${arg}" = "--search" ] && use_search=1
				[ "${arg}" = "--head" ] && use_head=1
				[ "${prev}" = "--jq" ] && jq_filter="${arg}"
				prev="${arg}"
			done
			local payload="[]"
			if [ "${use_head}" = 1 ]; then
				[ -n "${GH_PR_LIST_HEAD_FAIL:-}" ] && return 1
				payload="${GH_PR_LIST_HEAD_JSON:-[]}"
			elif [ "${use_search}" = 1 ]; then
				[ -n "${GH_PR_LIST_SEARCH_FAIL:-}" ] && return 1
				payload="${GH_PR_LIST_SEARCH_JSON:-[]}"
			fi
			if [ -n "${jq_filter}" ]; then
				printf '%s' "${payload}" | jq -r "${jq_filter}"
			else
				printf '%s' "${payload}"
			fi
			;;
		*)
			printf ''
			;;
	esac
	return 0
}

_issue_cross_ref_pr_numbers_unique() {
	[ -n "${CROSS_REF_FAIL:-}" ] && return 1
	[ -n "${CROSS_REF_PR_NUMBERS:-}" ] && printf '%s\n' "${CROSS_REF_PR_NUMBERS}"
	return 0
}
_issue_cross_ref_pr_number_last() { printf '%s' "${CROSS_REF_LAST:-}"; }

_pr_auto_merge_required_ci_check_name() { echo "${STUB_CI_NAME:-lint}"; }
_pr_checks_completed() {
	printf 'pr=%s sha=%s base=%s ids=%s\n' "$1" "$2" "$3" "${PR_CHECKS_CI_RUN_IDS-__unbound__}" >> "${CHECKS_LOG:-/dev/null}"
	PR_CHECKS_LAST_CI_STATE="${STUB_CI_STATE:-success}"
	return 0
}

recovery_action_for_phase() { echo "close_and_reissue"; }
phase_cap_state_for_action() { printf ''; }
phase_cap_can_dispatch() { return 0; }
tg_notify() { return 0; }
tg_notify_issue() { return 0; }
_gh_url() { printf 'https://github.com/%s/%s' "${GITHUB_REPOSITORY}" "$1"; }
ensure_label_exists() { return 0; }
count_noop_ancestors() { echo 0; }
memory_record_run_event() { return 0; }
"""


def _run_bash(script: str, cwd: Path) -> subprocess.CompletedProcess:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["GITHUB_REPOSITORY"] = "owner/repo"
	for name in (
		"STALL_REISSUE_GREEN_PR_GUARD_ENABLED",
		"STALL_MERGE_LEASE_WAIT_MAX_MINUTES",
		"MERGE_LEASE_WAIT_LABEL",
		"REVIEW_FINAL_CI_WORKFLOW",
		"AUTO_MERGE_REQUIRED_CI_CHECK",
	):
		env.pop(name, None)
	env["TMPDIR"] = str(cwd)
	return subprocess.run(["bash", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def _bootstrap(tmp: Path) -> None:
	extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
	lines = [f"extract_fn '{fn}' >> helpers.sh; echo >> helpers.sh" for fn in _FUNCTIONS]
	script = "set -euo pipefail\n" + extractor + "\n: > helpers.sh\n" + "\n".join(lines) + "\n"
	r = _run_bash(script, tmp)
	assert r.returncode == 0, r.stderr
	helpers = (tmp / "helpers.sh").read_text(encoding="utf-8")
	for fn in _FUNCTIONS:
		assert f"\n{fn}()" in "\n" + helpers, f"{fn} not extracted from {POLLER_SCRIPT}"
	check = _run_bash("bash -n helpers.sh", tmp)
	assert check.returncode == 0, check.stderr


def _q(text: str) -> str:
	return "'" + text.replace("'", "'\\''") + "'"


def _pr(number: int, head_ref: str, *, state: str = "open", labels: list[str] | None = None, body: str = "", **extra) -> dict:
	payload = {
		"number": number,
		"state": state,
		"merged": False,
		"draft": False,
		"title": f"AI implementation for PR {number}",
		"head": {"ref": head_ref, "sha": HEAD_SHA},
		"base": {"ref": "main"},
		"body": body,
		"labels": [{"name": name} for name in (labels or [])],
		"mergeable": True,
	}
	payload.update(extra)
	return payload


def _statuses(state: str) -> list[dict]:
	return [
		{"id": 1, "context": "ai-review/head-gate", "state": "failure", "created_at": "2026-11-21T08:00:00Z"},
		{"id": 2, "context": "ai-review/head-gate", "state": state, "created_at": "2026-11-21T09:00:00Z"},
		{"id": 3, "context": "other", "state": "failure", "created_at": "2026-11-21T09:30:00Z"},
	]


def _runs(path: str = ".github/workflows/ci.yml") -> dict:
	return {"workflow_runs": [{"id": 4242, "path": path, "head_sha": HEAD_SHA, "status": "completed", "conclusion": "success"}]}


def _invoke(
	tmp: Path,
	body: str,
	*,
	prs: dict[int, dict] | None = None,
	statuses: list | None = None,
	runs: dict | None = None,
	fail_paths: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
	api_lines = []
	for number, payload in (prs or {}).items():
		api_lines.append(f"API_JSON[repos/owner/repo/pulls/{number}]={_q(json.dumps(payload))}")
	if statuses is not None:
		api_lines.append(f"API_JSON[repos/owner/repo/commits/{HEAD_SHA}/statuses?per_page=100]={_q(json.dumps(statuses))}")
	if runs is not None:
		api_lines.append(
			f"API_JSON[repos/owner/repo/actions/workflows/ci.yml/runs?head_sha={HEAD_SHA}&per_page=20]={_q(json.dumps(runs))}"
		)
	for path in fail_paths:
		api_lines.append(f"API_FAIL[{path}]=1")
	script = "\n".join(
		[
			"set -o pipefail",
			"declare -gA API_JSON=()",
			"declare -gA API_FAIL=()",
			_STUBS,
			"source helpers.sh",
			f'export GH_LOG="{tmp}/gh.log" API_LOG="{tmp}/api.log" CHECKS_LOG="{tmp}/checks.log"',
			*api_lines,
			textwrap.dedent(body),
		]
	)
	return _run_bash(script, tmp)


_GUARD = """
rc=0
_stall_reissue_green_pr_guard 9398 130 || rc=$?
echo "RC=${rc} PR=${STALL_REISSUE_REFUSED_PR} REASON=${STALL_REISSUE_REFUSED_REASON}"
"""

_IMPL_ONLY = """
CROSS_REF_PR_NUMBERS="9402"
export GH_PR_LIST_HEAD_JSON='[{"number":9402}]'
"""


def _guard(tmp: Path, extra: str = "", **kwargs) -> subprocess.CompletedProcess:
	return _invoke(tmp, _IMPL_ONLY + extra + _GUARD, **kwargs)


def _with_tmp(fn):
	def wrapper():
		with tempfile.TemporaryDirectory() as td:
			tmp = Path(td)
			_bootstrap(tmp)
			fn(tmp)

	wrapper.__name__ = fn.__name__
	wrapper.__doc__ = fn.__doc__
	return wrapper


@_with_tmp
def test_guard_refuses_green_head(tmp: Path):
	"""head-gate success + a ci.yml-bound lint success refuses the close."""
	r = _guard(tmp, prs={9402: _pr(9402, "ai/issue-9398")}, statuses=_statuses("success"), runs=_runs())
	assert "RC=1 PR=9402 REASON=green_head" in r.stdout, (r.stdout, r.stderr)
	assert "STALL_REISSUE_REFUSED issue=9398 pr=9402 reason=green_head" in r.stdout, r.stdout
	checks = (tmp / "checks.log").read_text(encoding="utf-8")
	assert f"pr=9402 sha={HEAD_SHA} base=main ids=4242" in checks, checks


@_with_tmp
def test_guard_refuses_merge_lease_wait_without_api_reads(tmp: Path):
	r = _guard(tmp, prs={9402: _pr(9402, "ai/issue-9398", labels=["ai:merge-lease-wait"])})
	assert "RC=1 PR=9402 REASON=merge_lease_wait" in r.stdout, (r.stdout, r.stderr)
	api = (tmp / "api.log").read_text(encoding="utf-8")
	assert "statuses" not in api, api


@_with_tmp
def test_guard_refuses_draft(tmp: Path):
	r = _guard(tmp, prs={9402: _pr(9402, "ai/issue-9398", draft=True)})
	assert "RC=1 PR=9402 REASON=draft" in r.stdout, (r.stdout, r.stderr)


@_with_tmp
def test_guard_allows_when_head_gate_failing_or_pending(tmp: Path):
	for state in ("failure", "pending"):
		r = _guard(tmp, prs={9402: _pr(9402, "ai/issue-9398")}, statuses=_statuses(state), runs=_runs())
		assert "RC=0 PR= REASON=" in r.stdout, (state, r.stdout, r.stderr)
		assert "STALL_REISSUE_REFUSED" not in r.stdout, r.stdout


@_with_tmp
def test_guard_allows_when_lint_failing(tmp: Path):
	r = _guard(
		tmp,
		"export STUB_CI_STATE=failure\n",
		prs={9402: _pr(9402, "ai/issue-9398")},
		statuses=_statuses("success"),
		runs=_runs(),
	)
	assert "RC=0 PR= REASON=" in r.stdout, (r.stdout, r.stderr)


@_with_tmp
def test_guard_ignores_lint_from_another_workflow(tmp: Path):
	"""A `lint` that only comes from lint-plan-archival.yml is not CI: no
	ci.yml run is bound, so the head is not green and the close may run."""
	r = _guard(
		tmp,
		prs={9402: _pr(9402, "ai/issue-9398")},
		statuses=_statuses("success"),
		runs=_runs(".github/workflows/lint-plan-archival.yml"),
	)
	assert "RC=0 PR= REASON=" in r.stdout, (r.stdout, r.stderr)
	assert not (tmp / "checks.log").exists() or (tmp / "checks.log").read_text(encoding="utf-8") == ""


@_with_tmp
def test_guard_consumer_without_required_ci_needs_only_head_gate(tmp: Path):
	r = _guard(tmp, "export STUB_CI_NAME=none\n", prs={9402: _pr(9402, "ai/issue-9398")}, statuses=_statuses("success"))
	assert "RC=1 PR=9402 REASON=green_head" in r.stdout, (r.stdout, r.stderr)


@_with_tmp
def test_guard_fails_closed_on_read_errors(tmp: Path):
	statuses_path = f"repos/owner/repo/commits/{HEAD_SHA}/statuses?per_page=100"
	r = _guard(tmp, prs={9402: _pr(9402, "ai/issue-9398")}, fail_paths=(statuses_path,))
	assert "RC=1 PR=9402 REASON=check_unavailable" in r.stdout, (r.stdout, r.stderr)
	r = _guard(tmp, fail_paths=("repos/owner/repo/pulls/9402",))
	assert "RC=1 PR=9402 REASON=check_unavailable" in r.stdout, (r.stdout, r.stderr)
	r = _guard(tmp, "export GH_PR_LIST_HEAD_FAIL=1\n", prs={9402: _pr(9402, "ai/issue-9398")})
	assert "RC=1 PR= REASON=check_unavailable" in r.stdout, (r.stdout, r.stderr)
	assert "STALL_REISSUE_REFUSED issue=9398 pr=unknown reason=check_unavailable" in r.stdout, r.stdout
	r = _guard(tmp, "export CROSS_REF_FAIL=1\n", prs={9402: _pr(9402, "ai/issue-9398")})
	assert "RC=1 PR= REASON=check_unavailable" in r.stdout, (r.stdout, r.stderr)
	r = _guard(
		tmp,
		"export STUB_CI_STATE=unknown\n",
		prs={9402: _pr(9402, "ai/issue-9398")},
		statuses=_statuses("success"),
		runs=_runs(),
	)
	assert "RC=1 PR=9402 REASON=check_unavailable" in r.stdout, (r.stdout, r.stderr)


@_with_tmp
def test_guard_fails_closed_when_body_search_fails(tmp: Path):
	"""A failed body close-keyword search must refuse, not look like "no
	body-linked PRs" (the shared helper's fail-open `[]`)."""
	r = _invoke(
		tmp,
		"""
		CROSS_REF_PR_NUMBERS=""
		export GH_PR_LIST_HEAD_JSON='[]'
		export GH_PR_LIST_SEARCH_FAIL=1
		"""
		+ _GUARD,
	)
	assert "RC=1 PR= REASON=check_unavailable" in r.stdout, (r.stdout, r.stderr)


@_with_tmp
def test_guard_review_blocked_form_holds_lease_wait(tmp: Path):
	"""The review-blocked judge arm passes `0`: a lease-labelled PR whose
	head is not green is still refused with merge_lease_wait."""
	r = _invoke(
		tmp,
		_IMPL_ONLY
		+ """
		rc=0
		_stall_reissue_green_pr_guard 9398 0 || rc=$?
		echo "RC=${rc} PR=${STALL_REISSUE_REFUSED_PR} REASON=${STALL_REISSUE_REFUSED_REASON}"
		""",
		prs={9402: _pr(9402, "ai/issue-9398", labels=["ai:merge-lease-wait"])},
		statuses=_statuses("failure"),
	)
	assert "RC=1 PR=9402 REASON=merge_lease_wait" in r.stdout, (r.stdout, r.stderr)


@_with_tmp
def test_judge_does_not_hold_on_cross_ref_last_fallback(tmp: Path):
	"""A draft PR that only cites the issue (the cross_ref_last fallback) is
	not the issue's PR: it must not skip the judge on every tick."""
	r = _invoke(
		tmp,
		"""
		CROSS_REF_PR_NUMBERS="9700"
		CROSS_REF_LAST=9700
		export GH_PR_LIST_HEAD_JSON='[]'
		export GH_PR_LIST_SEARCH_JSON='[]'
		judge_rc=0
		invoke_stall_judge 9398 "ai:implementing" 2 130 "" </dev/null >judge.out 2>judge.err || judge_rc=$?
		cat judge.out
		""",
		prs={9700: _pr(9700, "claude/tooling", draft=True, body="Refs #9398")},
	)
	err = (tmp / "judge.err").read_text(encoding="utf-8")
	assert "STALL_JUDGE_LINKED_PR issue=9398 pr=9700 source=cross_ref_last" in err, err
	assert "source=judge_pick" not in r.stdout, r.stdout
	assert "run_stall_judge_skipped_pr_ai_hold" not in r.stdout, r.stdout


@_with_tmp
def test_guard_ignores_cross_reference_only_and_closed_prs(tmp: Path):
	r = _invoke(
		tmp,
		"""
		CROSS_REF_PR_NUMBERS=$'9685\\n9700'
		"""
		+ _GUARD,
		prs={
			9685: _pr(9685, "claude/unblock-chain-depth-cap", state="closed", body="Refs #9398"),
			9700: _pr(9700, "claude/tooling", labels=["ai:merge-lease-wait"], body="Refs #9398"),
		},
		statuses=_statuses("success"),
		runs=_runs(),
	)
	assert "RC=0 PR= REASON=" in r.stdout, (r.stdout, r.stderr)


@_with_tmp
def test_guard_kill_switch_allows(tmp: Path):
	r = _guard(
		tmp,
		"export STALL_REISSUE_GREEN_PR_GUARD_ENABLED=false\n",
		prs={9402: _pr(9402, "ai/issue-9398", labels=["ai:merge-lease-wait"])},
	)
	assert "RC=0 PR= REASON=" in r.stdout, (r.stdout, r.stderr)
	assert not (tmp / "api.log").exists() or (tmp / "api.log").read_text(encoding="utf-8") == ""


@_with_tmp
def test_guard_without_stall_clock_does_not_hold_lease_wait(tmp: Path):
	"""The one-argument form (no production caller) does not hold on the
	lease label. The review-blocked judge arm passes `0`
	(test_guard_review_blocked_form_holds_lease_wait), which does hold."""
	r = _invoke(
		tmp,
		_IMPL_ONLY
		+ """
		rc=0
		_stall_reissue_green_pr_guard 9398 || rc=$?
		echo "RC=${rc} PR=${STALL_REISSUE_REFUSED_PR} REASON=${STALL_REISSUE_REFUSED_REASON}"
		""",
		prs={9402: _pr(9402, "ai/issue-9398", labels=["ai:merge-lease-wait"])},
		statuses=_statuses("failure"),
	)
	assert "RC=0 PR= REASON=" in r.stdout, (r.stdout, r.stderr)


@_with_tmp
def test_replay_2026_11_21_issue_9398_keeps_pr_9402_open(tmp: Path):
	"""End to end: the judge's hold check names #9402 (not the last
	cross-ref #9685), and the close_and_reissue arm refuses with
	merge_lease_wait; no `gh pr close`, no issue close or ai:closed label."""
	prs = {
		9402: _pr(9402, "ai/issue-9398", labels=["ai:merge-lease-wait"], mergeable_state="clean"),
		9685: _pr(9685, "claude/unblock-chain-depth-cap", state="closed", body="Refs #9398"),
	}
	r = _invoke(
		tmp,
		"""
		CROSS_REF_PR_NUMBERS=$'9402\\n9685'
		CROSS_REF_LAST=9685
		export GH_PR_LIST_HEAD_JSON='[{"number":9402}]'
		export GH_PR_LIST_SEARCH_JSON='[]'
		judge_rc=0
		invoke_stall_judge 9398 "ai:done" 2 130 "" || judge_rc=$?
		echo "JUDGE_RC=${judge_rc} EFFECTIVE=${STALL_RECOVERY_EFFECTIVE_ACTION} INC=${STALL_RECOVERY_SHOULD_INCREMENT}"
		exec_rc=0
		execute_stall_recovery_action 9398 "ai:done" close_and_reissue 2 "" 130 || exec_rc=$?
		echo "EXEC_RC=${exec_rc} EFFECTIVE=${STALL_RECOVERY_EFFECTIVE_ACTION} INC=${STALL_RECOVERY_SHOULD_INCREMENT}"
		""",
		prs=prs,
		statuses=_statuses("success"),
		runs=_runs(),
	)
	out = r.stdout
	assert "STALL_JUDGE_LINKED_PR issue=9398 pr=9402 source=implementation candidates=9402,9685" in r.stderr, r.stderr
	assert (
		"STALL_SKIP issue=9398 reason=pr_ai_hold pr=9402 detail=merge_lease_wait phase=ai:done action=none source=judge_pick"
		in out
	), (out, r.stderr)
	assert "JUDGE_RC=1 EFFECTIVE=run_stall_judge_skipped_pr_ai_hold INC=false" in out, out
	assert "STALL_REISSUE_REFUSED issue=9398 pr=9402 reason=merge_lease_wait" in out, out
	assert "EXEC_RC=1 EFFECTIVE=close_and_reissue_refused INC=false" in out, out
	gh_log = (tmp / "gh.log").read_text(encoding="utf-8") if (tmp / "gh.log").exists() else ""
	assert "pr close" not in gh_log, gh_log
	assert "issue close" not in gh_log, gh_log
	assert "ai:closed" not in gh_log, gh_log
	assert "issue create" not in gh_log, gh_log


def test_close_and_reissue_arms_call_the_guard():
	"""All three close_and_reissue arms run the guard first."""
	text = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert text.count('_stall_reissue_green_pr_guard "${issue_num}" "${stall_minutes}"') == 1
	assert text.count('_stall_reissue_green_pr_guard "${issue_num}" "${elapsed_minutes}"') == 1
	assert text.count('_stall_reissue_green_pr_guard "${rb_issue}" "0"') == 1
	assert text.count('STALL_RECOVERY_EFFECTIVE_ACTION="close_and_reissue_refused"') == 2


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	passed = failed = 0
	for func in test_funcs:
		try:
			func()
			print(f"  PASS  {func.__name__}")
			passed += 1
		except Exception as exc:  # noqa: BLE001 - report every failure
			print(f"  FAIL  {func.__name__}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
