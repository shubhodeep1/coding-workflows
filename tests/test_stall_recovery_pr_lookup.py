#!/usr/bin/env python3
"""Runtime tests for the stall-recovery PR-lookup fixes.

Covers:
- Gap 1: ``close_linked_pr`` discovers linked PRs via timeline cross-ref,
  branch-name (``ai/issue-<n>``) and body-parse (``Closes #<n>``), iterates
  every open match, skips already-closed PRs, and logs a diagnostic when
  no PR is found.
- Gap 2: ``surface_reissue_closed_without_pr`` emits the
  ``REISSUE_CLOSED_WITHOUT_PR`` log prefix, the GHA ``::warning::``
  annotation, and posts an issue comment — only when the issue body
  carries the ``Re-issued from #<n>`` marker and no PR is found.

Uses the same function-extraction-plus-stub pattern as
``test_merge_probe.py``: pull the helper definitions out of the
production script with awk, source them, and exercise with a controlled
``gh`` / ``gh_retry`` / ``_safe_gh_jq`` environment.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	full_env.setdefault("GITHUB_REPOSITORY", "owner/repo")
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=full_env,
		capture_output=True,
		text=True,
	)


# Extracts a single shell function body from POLLER_SCRIPT by matching
# the declaration line (``fn()`` at column 0) through the first
# closing ``}`` at column 0. Works for both ``fn() {`` and ``fn()\n{``
# styles since we just scan forward from the declaration until we hit
# a bare ``}`` line.
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
# Minimal stubs for the helper dependencies we do not want to exercise
# from the production code path. Each test injects richer behavior by
# overriding individual stubs *after* sourcing the helpers.

gh_retry() { "$@"; }

_safe_gh_jq() {
	# Dispatched by ENDPOINT_STATES / ENDPOINT_BODIES associative arrays
	# exported from the test harness. Signature mirrors production:
	#   _safe_gh_jq "<path>" --jq '<filter>'
	local path="$1"
	shift
	local jq_filter=""
	while [ "$#" -gt 0 ]; do
		if [ "$1" = "--jq" ]; then
			jq_filter="$2"
			shift 2
		else
			shift
		fi
	done
	case "${jq_filter}" in
		".state")
			printf '%s' "${PR_STATE_MAP[${path}]:-open}"
			;;
		".body // \"\""|".body // \"\"")
			printf '%s' "${ISSUE_BODY_MAP[${path}]:-}"
			;;
		*)
			printf ''
			;;
	esac
}

# Default gh shim — tests override subcommands as needed. Honors --jq
# the same way the real gh does (pipes JSON through jq -r), so callers
# that rely on ``gh pr list --jq '.[].number'`` get numbers, not JSON.
gh() {
	local sub="${1:-}"
	local sub2="${2:-}"
	case "${sub}-${sub2}" in
		pr-list)
			local use_search=0
			local use_head=0
			local jq_filter=""
			local prev=""
			for arg in "$@"; do
				[ "${arg}" = "--search" ] && use_search=1
				[ "${arg}" = "--head" ] && use_head=1
				[ "${prev}" = "--jq" ] && jq_filter="${arg}"
				prev="${arg}"
			done
			local payload=""
			if [ "${use_search}" = 1 ]; then
				payload="${GH_PR_LIST_SEARCH_JSON:-[]}"
			elif [ "${use_head}" = 1 ]; then
				payload="${GH_PR_LIST_HEAD_JSON:-[]}"
			else
				payload="[]"
			fi
			if [ -n "${jq_filter}" ]; then
				printf '%s' "${payload}" | jq -r "${jq_filter}" 2>/dev/null || true
			else
				printf '%s' "${payload}"
			fi
			;;
		pr-close)
			printf '%s\n' "$3" >> "${GH_PR_CLOSE_LOG:-/dev/null}"
			;;
		api-*)
			local path="$2"
			if [[ "${path}" == *"/comments" ]]; then
				printf '%s\n' "${path}" >> "${GH_ISSUE_COMMENT_LOG:-/dev/null}"
			fi
			printf ''
			;;
		*)
			printf ''
			;;
	esac
	return 0
}

# Stub the timeline → cross-ref helper that upstream helpers call
# through when branch-name / body-parse return empty. Tests override
# ``_issue_cross_ref_pr_numbers_unique`` directly below. Emits newline-
# delimited numbers (one per line) to match the real helper's output
# shape — critical so the ``grep -E '^[0-9]+$'`` filter in
# ``_find_all_linked_prs`` sees each entry on its own line.
_issue_timeline_with_cross_refs_json() { printf '[]'; }
_issue_cross_ref_pr_numbers_unique() {
	local raw="${CROSS_REF_PR_NUMBERS:-}"
	[ -z "${raw}" ] && return 0
	printf '%s\n' "${raw}"
}

memory_record_run_event() { return 0; }

export -f gh gh_retry _safe_gh_jq _issue_timeline_with_cross_refs_json _issue_cross_ref_pr_numbers_unique memory_record_run_event 2>/dev/null || true
"""


def _bootstrap(tmp_dir: Path) -> Path:
	"""Write ``helpers.sh`` containing the extracted production helpers
	plus the common stubs, and return the path."""
	extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
	script = textwrap.dedent(f"""
	set -euo pipefail
	{extractor}
	: > helpers.sh
	extract_fn '_linked_prs_by_branch_name'    >> helpers.sh
	echo                                       >> helpers.sh
	extract_fn '_linked_prs_by_body_reference' >> helpers.sh
	echo                                       >> helpers.sh
	extract_fn '_find_all_linked_prs'          >> helpers.sh
	echo                                       >> helpers.sh
	extract_fn 'close_linked_pr'               >> helpers.sh
	echo                                       >> helpers.sh
	extract_fn 'surface_reissue_closed_without_pr' >> helpers.sh
	""")
	r = _run_bash(script, cwd=tmp_dir)
	assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
	helpers = tmp_dir / "helpers.sh"
	assert helpers.exists() and helpers.stat().st_size > 0, "helpers.sh not produced"
	return helpers


def _invoke(body: str, cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	script = textwrap.dedent(f"""
	set -uo pipefail
	declare -gA PR_STATE_MAP=()
	declare -gA ISSUE_BODY_MAP=()
	{_STUBS}
	source helpers.sh
	{body}
	""")
	return _run_bash(script, cwd=cwd, env=extra_env)


# ---------------------------------------------------------------------------
# Gap 1: close_linked_pr broadened lookup
# ---------------------------------------------------------------------------

def test_close_linked_pr_finds_pr_via_branch_name_when_timeline_empty():
	"""Reproduces the #2552 / #2568 prod miss: timeline cross-ref event is
	missing, but the PR exists at branch ``ai/issue-2552``. The broadened
	lookup must discover and close it."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		log = tmp / "closed.log"
		log.write_text("", encoding="utf-8")
		comment_log = tmp / "comments.log"
		comment_log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_PR_CLOSE_LOG="{log}"
			export GH_ISSUE_COMMENT_LOG="{comment_log}"
			CROSS_REF_PR_NUMBERS=""
			export GH_PR_LIST_HEAD_JSON='[{{"number":2568}}]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			PR_STATE_MAP["repos/owner/repo/pulls/2568"]="open"
			close_linked_pr 2552 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "2568" in log.read_text(encoding="utf-8"), (
			f"expected PR 2568 to be closed; log was {log.read_text()}\nstdout:\n{r.stdout}"
		)
		assert "close_linked_pr: closing linked PR #2568" in r.stdout
		assert "scanned=1 closed=1" in r.stdout


def test_close_linked_pr_finds_pr_via_body_parse_when_branch_empty():
	"""Body-parse fallback: timeline and branch-name return empty, but a
	PR body says ``Closes #2552``. Must be discovered and closed."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		log = tmp / "closed.log"
		log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_PR_CLOSE_LOG="{log}"
			CROSS_REF_PR_NUMBERS=""
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[{{"number":2580,"body":"Closes #2552"}},{{"number":2581,"body":"mentions #25528 only"}}]'
			PR_STATE_MAP["repos/owner/repo/pulls/2580"]="open"
			PR_STATE_MAP["repos/owner/repo/pulls/2581"]="open"
			close_linked_pr 2552 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		text = log.read_text(encoding="utf-8")
		assert "2580" in text, f"expected #2580 closed; got: {text}"
		assert "2581" not in text, (
			f"#25528 substring must not match issue #2552 (word-boundary regression); got: {text}"
		)


def test_close_linked_pr_iterates_multiple_prs():
	"""Two distinct PRs linked to the issue — both must be closed."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		log = tmp / "closed.log"
		log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_PR_CLOSE_LOG="{log}"
			CROSS_REF_PR_NUMBERS=$'3001\n3002'
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			PR_STATE_MAP["repos/owner/repo/pulls/3001"]="open"
			PR_STATE_MAP["repos/owner/repo/pulls/3002"]="open"
			close_linked_pr 2552 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		closed = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
		assert sorted(closed) == ["3001", "3002"], closed
		assert "scanned=2 closed=2" in r.stdout


def test_close_linked_pr_dedupes_across_lookup_sources():
	"""A PR surfaced by BOTH timeline and branch-name must only be
	closed once."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		log = tmp / "closed.log"
		log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_PR_CLOSE_LOG="{log}"
			CROSS_REF_PR_NUMBERS="2568"
			export GH_PR_LIST_HEAD_JSON='[{{"number":2568}}]'
			export GH_PR_LIST_SEARCH_JSON='[{{"number":2568,"body":"Closes #2552"}}]'
			PR_STATE_MAP["repos/owner/repo/pulls/2568"]="open"
			close_linked_pr 2552 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		closed = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
		assert closed == ["2568"], closed


def test_close_linked_pr_skips_already_closed_pr():
	"""If the discovered PR is already closed/merged, no ``pr close`` is
	issued."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		log = tmp / "closed.log"
		log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_PR_CLOSE_LOG="{log}"
			CROSS_REF_PR_NUMBERS="4001"
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			PR_STATE_MAP["repos/owner/repo/pulls/4001"]="merged"
			close_linked_pr 2552 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert log.read_text(encoding="utf-8").strip() == ""
		assert "skipping PR #4001" in r.stdout
		assert "state=merged" in r.stdout


def test_close_linked_pr_logs_miss_when_no_sources_return_a_pr():
	"""When all three lookups are empty, log the diagnostic and return 0."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body="""
			CROSS_REF_PR_NUMBERS=""
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			close_linked_pr 9999 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "no linked PRs found for issue #9999" in r.stderr
		assert "timeline/branch/body lookups all empty" in r.stderr


# ---------------------------------------------------------------------------
# Gap 2: surface_reissue_closed_without_pr
# ---------------------------------------------------------------------------


def test_surface_fires_on_reissue_body_with_no_pr():
	"""Reproduces #2591: body marks this as a re-issue of #2552 and no PR
	exists. Must emit the stable log prefix, GHA warning, and post an
	issue comment."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		comment_log = tmp / "comments.log"
		comment_log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_ISSUE_COMMENT_LOG="{comment_log}"
			ISSUE_BODY_MAP["repos/owner/repo/issues/2591"]=$'Original body\\n\\n---\\n\\n**Re-issued from #2552** — stalled in ai:done for 127m.'
			CROSS_REF_PR_NUMBERS=""
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			surface_reissue_closed_without_pr 2591 "ai:done" 130 3 "standalone"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "REISSUE_CLOSED_WITHOUT_PR issue=2591 parent=2552 phase=ai:done stall_minutes=130 recovery_count=3 source=standalone" in r.stdout
		assert "::warning title=Re-issue closed without PR::" in r.stdout
		comments = comment_log.read_text(encoding="utf-8").strip().splitlines()
		assert any("/issues/2591/comments" in c for c in comments), comments


def test_surface_noops_when_issue_is_not_a_reissue():
	"""An ordinary (non-re-issue) task closing with no PR is out-of-scope
	for Gap 2. The helper must stay silent."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		comment_log = tmp / "comments.log"
		comment_log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_ISSUE_COMMENT_LOG="{comment_log}"
			ISSUE_BODY_MAP["repos/owner/repo/issues/7"]="plain task body with no re-issue marker"
			CROSS_REF_PR_NUMBERS=""
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			surface_reissue_closed_without_pr 7 "ai:done" 130 3 "main"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "REISSUE_CLOSED_WITHOUT_PR" not in r.stdout
		assert comment_log.read_text(encoding="utf-8").strip() == ""


def test_surface_noops_when_reissue_has_at_least_one_pr():
	"""Re-issue that actually produced a PR is healthy — no Gap-2
	signal."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		comment_log = tmp / "comments.log"
		comment_log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_ISSUE_COMMENT_LOG="{comment_log}"
			ISSUE_BODY_MAP["repos/owner/repo/issues/2591"]="**Re-issued from #2552** stalled"
			CROSS_REF_PR_NUMBERS="2700"
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			surface_reissue_closed_without_pr 2591 "ai:done" 130 3 "standalone"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "REISSUE_CLOSED_WITHOUT_PR" not in r.stdout
		assert comment_log.read_text(encoding="utf-8").strip() == ""
