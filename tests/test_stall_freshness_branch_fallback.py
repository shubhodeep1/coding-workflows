#!/usr/bin/env python3
"""Runtime tests for the two-layer stall-freshness branch fallback.

Background: both stall-freshness guards — the ``detect_stalls`` ai:done
clock re-anchor (Layer 2) and ``_check_fresh_push_guard`` (Layer 1) —
derived the linked PR's ``headPushedAt`` from the single issue→PR
cross-reference timeline. When that cross-reference is transiently
suppressed (issue #2552 / PR #2568 class) both guards fail open in
lock-step and a PR that was just pushed is reported "stuck" and given an
unnecessary ``retrigger_review``.

The fix adds a deterministic ``ai/issue-<n>`` head-branch fallback —
``_resolve_linked_pr_fresh_by_branch`` — and wires it into:
  - Layer 1 via ``_check_fresh_push_guard_with_fallback`` (used by both
    ``recover_stalled_issue`` and ``run_standalone_stall_recovery``), and
  - Layer 2 via a selection filter that re-resolves only the ai:done /
    ai:ready-to-merge wave issues whose primary headPushedAt is missing.

Uses the same function-extraction-plus-stub pattern as
``test_stall_recovery_pr_lookup.py``: pull the helper definitions out of
the production script with awk, source them, and exercise with a
controlled ``gh`` / ``gh_retry`` environment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _iso_utc_minutes_ago(minutes: int) -> str:
	return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	full_env["GITHUB_REPOSITORY"] = "owner/repo"
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=full_env,
		capture_output=True,
		text=True,
	)


# Extract a single shell function body from POLLER_SCRIPT: match the
# declaration line (``fn()`` at column 0) through the first closing ``}``
# at column 0. Handles both ``fn() {`` and ``fn()\n{`` styles.
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


# Minimal stubs. ``gh pr list --head ... --json number,commits`` (no --jq)
# returns GH_PR_LIST_HEAD_JSON verbatim so the helper's own jq parses it,
# mirroring real ``gh`` behaviour for the field-list form.
_STUBS = r"""
gh_retry() { "$@"; }

gh() {
	local is_list=0 use_head=0
	for arg in "$@"; do
		[ "${arg}" = "list" ] && is_list=1
		[ "${arg}" = "--head" ] && use_head=1
	done
	if [ "${is_list}" = 1 ] && [ "${use_head}" = 1 ]; then
		printf '%s' "${GH_PR_LIST_HEAD_JSON:-[]}"
		return 0
	fi
	printf ''
	return 0
}

export -f gh gh_retry 2>/dev/null || true
"""


def _bootstrap(tmp_dir: Path) -> Path:
	"""Write ``helpers.sh`` with the three production helpers, return its path."""
	extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
	script = textwrap.dedent(f"""
	set -euo pipefail
	{extractor}
	: > helpers.sh
	# _FRESH_PUSH_SUPPRESS_SECS is a module-level constant in the poller, not
	# part of any extracted function body — pull the real value so the guard's
	# window matches production exactly (and survives a future retune).
	grep -E '^_FRESH_PUSH_SUPPRESS_SECS=' '{POLLER_SCRIPT}'   >> helpers.sh
	echo                                                 >> helpers.sh
	extract_fn '_resolve_linked_pr_fresh_by_branch'      >> helpers.sh
	echo                                                 >> helpers.sh
	extract_fn '_check_fresh_push_guard'                 >> helpers.sh
	echo                                                 >> helpers.sh
	extract_fn '_check_fresh_push_guard_with_fallback'   >> helpers.sh
	""")
	r = _run_bash(script, cwd=tmp_dir)
	assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
	helpers = tmp_dir / "helpers.sh"
	assert helpers.exists() and helpers.stat().st_size > 0, "helpers.sh not produced"
	# Sanity: every helper landed in the extract.
	body = helpers.read_text(encoding="utf-8")
	for fn in (
		"_resolve_linked_pr_fresh_by_branch()",
		"_check_fresh_push_guard()",
		"_check_fresh_push_guard_with_fallback()",
	):
		assert fn in body, f"missing {fn} in helpers.sh"
	return helpers


def _invoke(body: str, cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	script = textwrap.dedent(f"""
	set -uo pipefail
	{_STUBS}
	source helpers.sh
	{body}
	""")
	return _run_bash(script, cwd=cwd, env=extra_env)


# ---------------------------------------------------------------------------
# _resolve_linked_pr_fresh_by_branch
# ---------------------------------------------------------------------------

def test_resolver_returns_number_and_max_committed_date():
	"""Resolves the open ai/issue-<n> PR and reports the NEWEST commit's
	committedDate as headPushedAt (max over the commits array, order-
	independent)."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			export GH_PR_LIST_HEAD_JSON="$(jq -cn --arg d "$FRESH_ISO" \
				'[{number:4242,commits:[{committedDate:"2020-01-01T00:00:00Z"},{committedDate:$d}]}]')"
			out="$(_resolve_linked_pr_fresh_by_branch 4242)"
			echo "OUT=${out}"
			echo "WANT_ISO=${FRESH_ISO}"
			""",
			cwd=tmp,
			extra_env={"FRESH_ISO": _iso_utc_minutes_ago(10)},
		)
		assert r.returncode == 0, r.stderr
		lines = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
		want_iso = lines["WANT_ISO"]
		assert '"number":4242' in lines["OUT"], r.stdout
		assert want_iso in lines["OUT"], r.stdout


def test_resolver_empty_when_no_open_pr():
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			export GH_PR_LIST_HEAD_JSON='[]'
			out="$(_resolve_linked_pr_fresh_by_branch 4242)"
			echo "OUT=[${out}]"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "OUT=[]" in r.stdout, r.stdout


def test_resolver_empty_when_pr_has_no_commits():
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			export GH_PR_LIST_HEAD_JSON='[{"number":4242,"commits":[]}]'
			out="$(_resolve_linked_pr_fresh_by_branch 4242)"
			echo "OUT=[${out}]"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "OUT=[]" in r.stdout, r.stdout


def test_resolver_rejects_non_numeric_issue():
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			export GH_PR_LIST_HEAD_JSON='[{"number":1,"commits":[{"committedDate":"2026-01-01T00:00:00Z"}]}]'
			out="$(_resolve_linked_pr_fresh_by_branch 'not-a-number')"
			echo "OUT=[${out}]"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "OUT=[]" in r.stdout, r.stdout


# ---------------------------------------------------------------------------
# _check_fresh_push_guard_with_fallback
# ---------------------------------------------------------------------------

def test_wrapper_primary_fresh_suppresses_via_cross_ref_without_branch_lookup():
	"""When the primary cross-ref entry is fresh, the inner guard fires and
	the branch fallback is never consulted (source stays cross_ref, no
	STALL_FRESH_PUSH_FALLBACK diagnostic)."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			entry="$(jq -cn --arg d "$FRESH_ISO" '{number:11,state:"OPEN",merged:false,headPushedAt:$d}')"
			# Branch would resolve nothing; it must NOT be consulted.
			export GH_PR_LIST_HEAD_JSON='[]'
			if _check_fresh_push_guard_with_fallback 11 "$entry" "ai:done"; then echo "RC=0"; else echo "RC=1"; fi
			echo "SRC=${FRESH_PUSH_SOURCE}"
			echo "PR=${FRESH_PUSH_PR_NUM}"
			""",
			cwd=tmp,
			extra_env={"FRESH_ISO": _iso_utc_minutes_ago(5)},
		)
		assert r.returncode == 0, r.stderr
		assert "RC=0" in r.stdout, r.stdout
		assert "SRC=cross_ref" in r.stdout, r.stdout
		assert "PR=11" in r.stdout, r.stdout
		assert "STALL_FRESH_PUSH_FALLBACK" not in r.stdout, r.stdout


def test_wrapper_primary_null_branch_fresh_suppresses_via_branch_fallback():
	"""The core false-positive fix: primary cross-ref entry is null, but the
	deterministic branch lookup finds a freshly-pushed PR — recovery is
	suppressed and attributed to the branch fallback."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			export GH_PR_LIST_HEAD_JSON="$(jq -cn --arg d "$FRESH_ISO" '[{number:22,commits:[{committedDate:$d}]}]')"
			if _check_fresh_push_guard_with_fallback 22 "null" "ai:done"; then echo "RC=0"; else echo "RC=1"; fi
			echo "SRC=${FRESH_PUSH_SOURCE}"
			echo "PR=${FRESH_PUSH_PR_NUM}"
			""",
			cwd=tmp,
			extra_env={"FRESH_ISO": _iso_utc_minutes_ago(5)},
		)
		assert r.returncode == 0, r.stderr
		assert "RC=0" in r.stdout, r.stdout
		assert "SRC=branch_fallback" in r.stdout, r.stdout
		assert "PR=22" in r.stdout, r.stdout
		assert "STALL_FRESH_PUSH_FALLBACK issue=22 phase=ai:done source=branch_name resolved=" in r.stdout, r.stdout
		assert "resolved=none" not in r.stdout, r.stdout


def test_wrapper_primary_null_branch_stale_does_not_suppress():
	"""Branch resolves a PR but its head commit is old (genuine stall) — the
	guard must NOT suppress, so the recovery proceeds."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			export GH_PR_LIST_HEAD_JSON="$(jq -cn --arg d "$STALE_ISO" '[{number:33,commits:[{committedDate:$d}]}]')"
			if _check_fresh_push_guard_with_fallback 33 "null" "ai:done"; then echo "RC=0"; else echo "RC=1"; fi
			""",
			cwd=tmp,
			extra_env={"STALE_ISO": _iso_utc_minutes_ago(120)},
		)
		assert r.returncode == 0, r.stderr
		assert "RC=1" in r.stdout, r.stdout
		assert "STALL_FRESH_PUSH_FALLBACK issue=33 phase=ai:done source=branch_name resolved=" in r.stdout, r.stdout


def test_wrapper_primary_null_branch_empty_logs_resolved_none():
	"""No PR resolvable by branch at all — guard does not suppress and the
	non-silenced diagnostic records resolved=none for post-hoc tracing."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			export GH_PR_LIST_HEAD_JSON='[]'
			if _check_fresh_push_guard_with_fallback 44 "null" "ai:done"; then echo "RC=0"; else echo "RC=1"; fi
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "RC=1" in r.stdout, r.stdout
		assert "STALL_FRESH_PUSH_FALLBACK issue=44 phase=ai:done source=branch_name resolved=none" in r.stdout, r.stdout


def test_wrapper_ineligible_phase_skips_fallback():
	"""For phases outside {ai:done, ai:ready-to-merge} the inner guard short-
	circuits and the branch fallback is not attempted."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			export GH_PR_LIST_HEAD_JSON="$(jq -cn --arg d "$FRESH_ISO" '[{number:55,commits:[{committedDate:$d}]}]')"
			if _check_fresh_push_guard_with_fallback 55 "null" "ai:implementing"; then echo "RC=0"; else echo "RC=1"; fi
			""",
			cwd=tmp,
			extra_env={"FRESH_ISO": _iso_utc_minutes_ago(5)},
		)
		assert r.returncode == 0, r.stderr
		assert "RC=1" in r.stdout, r.stdout
		assert "STALL_FRESH_PUSH_FALLBACK" not in r.stdout, r.stdout


def test_wrapper_primary_present_but_stale_does_not_trigger_branch_lookup():
	"""When the primary entry HAS a headPushedAt that is merely old (a real
	stall), the wrapper must not burn a branch lookup even if the branch
	would resolve a fresh push — it returns 1 with no fallback diagnostic.
	Locks in the 'only re-resolve when primary missing' optimization."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			entry="$(jq -cn --arg d "$STALE_ISO" '{number:66,state:"OPEN",merged:false,headPushedAt:$d}')"
			# Branch is fresh — but must be ignored because the primary entry
			# already carried a (stale) headPushedAt.
			export GH_PR_LIST_HEAD_JSON="$(jq -cn --arg d "$FRESH_ISO" '[{number:66,commits:[{committedDate:$d}]}]')"
			if _check_fresh_push_guard_with_fallback 66 "$entry" "ai:done"; then echo "RC=0"; else echo "RC=1"; fi
			echo "SRC=${FRESH_PUSH_SOURCE}"
			""",
			cwd=tmp,
			extra_env={
				"STALE_ISO": _iso_utc_minutes_ago(200),
				"FRESH_ISO": _iso_utc_minutes_ago(2),
			},
		)
		assert r.returncode == 0, r.stderr
		assert "RC=1" in r.stdout, r.stdout
		assert "SRC=cross_ref" in r.stdout, r.stdout
		assert "STALL_FRESH_PUSH_FALLBACK" not in r.stdout, r.stdout


def test_wrapper_ready_to_merge_phase_eligible():
	"""ai:ready-to-merge is also a PR-bearing phase and must use the fallback."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body=r"""
			export GH_PR_LIST_HEAD_JSON="$(jq -cn --arg d "$FRESH_ISO" '[{number:77,commits:[{committedDate:$d}]}]')"
			if _check_fresh_push_guard_with_fallback 77 "null" "ai:ready-to-merge"; then echo "RC=0"; else echo "RC=1"; fi
			echo "SRC=${FRESH_PUSH_SOURCE}"
			""",
			cwd=tmp,
			extra_env={"FRESH_ISO": _iso_utc_minutes_ago(5)},
		)
		assert r.returncode == 0, r.stderr
		assert "RC=0" in r.stdout, r.stdout
		assert "SRC=branch_fallback" in r.stdout, r.stdout


# ---------------------------------------------------------------------------
# Layer-2 selection filter (the jq that picks which wave issues get the
# branch re-resolution before check-stalls runs). Mirrors the filter inlined
# in the stall block so its gating is regression-tested.
# ---------------------------------------------------------------------------

_REANCHOR_SELECT_FILTER = r"""
to_entries[]
| select((.value.labels // []) | any(. == "ai:done" or . == "ai:ready-to-merge"))
| select(
    (.value.linked_pr == null)
    or ((.value.linked_pr | type) != "object")
    or (.value.linked_pr.headPushedAt == null)
    or ((.value.linked_pr.headPushedAt | type) != "string")
    or ((.value.linked_pr.headPushedAt | length) == 0)
  )
| .key
"""


def test_reanchor_selection_filter_picks_only_missing_pr_bearing_issues():
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		wave = (
			'{'
			'"100":{"labels":["ai:done"],"linked_pr":null},'
			'"101":{"labels":["ai:done"],"linked_pr":{"headPushedAt":"2026-06-01T00:00:00Z"}},'
			'"102":{"labels":["ai:ready-to-merge"],"linked_pr":{"headPushedAt":null}},'
			'"103":{"labels":["ai:implementing"],"linked_pr":null},'
			'"104":{"labels":["ai:done"],"linked_pr":{"headPushedAt":""}}'
			'}'
		)
		filt = _REANCHOR_SELECT_FILTER.replace('"', r'\"')
		r = _invoke(
			body=f"""
			WAVE='{wave}'
			printf '%s' "$WAVE" | jq -r "{filt}" | sort | tr '\\n' ' '
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		selected = r.stdout.split()
		assert selected == ["100", "102", "104"], f"got {selected}\n{r.stdout}\n{r.stderr}"
