#!/usr/bin/env python3
"""Runtime tests for `_subissue_closing_pr_number`.

`_subissue_closing_pr_number` resolves the PR that actually implemented
and merged a sub-issue, for orchestrator intent-fingerprint capture
(`capture_intent_fingerprints_for_merged_subissue`). The pre-2026-05
capture call sites picked the *last* cross-referenced PR on the
sub-issue's timeline — which latched onto an unrelated `Refs #N`
infrastructure PR whenever one referenced the issue last, fingerprinting
that PR's diff lines and wedging the wave-dispatch fingerprint gate.

Regression anchor: project #2867 wave-2 dispatch blocked because issue
#2872 (never implemented; only `Refs #2872` cross-references from PRs
#2889 and #2894) had fingerprints captured from open PR #2894.

Uses the same function-extraction-plus-stub pattern as
`test_stall_recovery_pr_lookup.py`: pull the helper definition out of
the production script with awk, source it, and exercise it with a
controlled `gh` / `gh_retry` / `_issue_cross_ref_pr_numbers_unique`
environment.
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


# Extract a single shell function body from POLLER_SCRIPT by matching the
# declaration line (``fn()`` at column 0) through the first closing ``}``
# at column 0 — identical to test_stall_recovery_pr_lookup.py.
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


# Stubs for the helper's dependencies.
#   * gh_retry         — pass-through.
#   * gh pr list       — replays MOCK_PR_LIST_HEAD_JSON, honoring --jq.
#   * gh api pulls/<n> — replays the matching object from MOCK_PR_JSON.
#   * _issue_cross_ref_pr_numbers_unique — newline-delimited MOCK_XREF_PRS.
_STUBS = r"""
gh_retry() { "$@"; }

gh() {
	local sub="${1:-}" sub2="${2:-}"
	if [ "${sub}" = "pr" ] && [ "${sub2}" = "list" ]; then
		local jq_filter="" prev=""
		for arg in "$@"; do
			[ "${prev}" = "--jq" ] && jq_filter="${arg}"
			prev="${arg}"
		done
		local payload="${MOCK_PR_LIST_HEAD_JSON:-[]}"
		if [ -n "${jq_filter}" ]; then
			printf '%s' "${payload}" | jq -r "${jq_filter}" 2>/dev/null || true
		else
			printf '%s' "${payload}"
		fi
		return 0
	fi
	if [ "${sub}" = "api" ]; then
		local path="${2:-}"
		local num="${path##*/}"
		printf '%s' "${MOCK_PR_JSON:-{}}" | jq -c --arg n "${num}" '.[$n] // empty' 2>/dev/null || true
		return 0
	fi
	return 0
}

_issue_cross_ref_pr_numbers_unique() {
	local raw="${MOCK_XREF_PRS:-}"
	[ -z "${raw}" ] && return 0
	printf '%s\n' "${raw}"
}

export -f gh gh_retry _issue_cross_ref_pr_numbers_unique 2>/dev/null || true
"""


def _bootstrap(tmp_dir: Path) -> None:
	"""Write ``helpers.sh`` containing the extracted production helper."""
	extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
	script = textwrap.dedent(f"""
	set -euo pipefail
	{extractor}
	: > helpers.sh
	extract_fn '_subissue_closing_pr_number' >> helpers.sh
	""")
	r = _run_bash(script, cwd=tmp_dir)
	assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
	helpers = tmp_dir / "helpers.sh"
	assert helpers.exists() and helpers.stat().st_size > 0, "helpers.sh not produced"


def _resolve(
	issue_num: int,
	*,
	head_prs: list[dict] | None = None,
	xref_prs: list[int] | None = None,
	pr_json: dict[int, dict] | None = None,
) -> str:
	"""Run `_subissue_closing_pr_number <issue_num>` under the stubs and
	return its trimmed stdout (the resolved PR number, or "")."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		env = {
			"MOCK_PR_LIST_HEAD_JSON": json.dumps(head_prs or []),
			"MOCK_XREF_PRS": "\n".join(str(n) for n in (xref_prs or [])),
			"MOCK_PR_JSON": json.dumps({str(k): v for k, v in (pr_json or {}).items()}),
		}
		script = textwrap.dedent(f"""
		set -uo pipefail
		{_STUBS}
		source helpers.sh
		_subissue_closing_pr_number {issue_num}
		""")
		r = _run_bash(script, cwd=tmp, env=env)
		assert r.returncode == 0, f"helper exited non-zero: {r.returncode}\n{r.stderr}"
		return r.stdout.strip()


# ---------------------------------------------------------------------------
# Tier 1 — conventional ai/issue-<n> implementation branch
# ---------------------------------------------------------------------------


def test_tier1_selects_merged_ai_issue_branch_pr():
	"""A merged PR on the orchestrator's `ai/issue-<n>` branch is the
	deterministic implementation PR and must be selected directly."""
	out = _resolve(
		2873,
		head_prs=[{"number": 2878, "mergedAt": "2026-05-22T05:31:30Z"}],
	)
	assert out == "2878", out


def test_tier1_picks_most_recently_merged_when_multiple():
	"""If a sub-issue was re-implemented, several merged PRs may carry the
	`ai/issue-<n>` branch name over time. The most-recently-merged one is
	the final implementation."""
	out = _resolve(
		2873,
		head_prs=[
			{"number": 2800, "mergedAt": "2026-05-01T00:00:00Z"},
			{"number": 2878, "mergedAt": "2026-05-22T05:31:30Z"},
		],
	)
	assert out == "2878", out


# ---------------------------------------------------------------------------
# Regression — project #2867 / issue #2872: Refs-only cross-references
# ---------------------------------------------------------------------------


def test_refs_only_cross_references_yield_no_pr():
	"""THE #2872 reproduction. Issue #2872 was never implemented — it has
	no `ai/issue-2872` branch, only `Refs #2872` cross-references from a
	merged infrastructure PR (#2889) and an open PR (#2894). The helper
	must return empty so capture is skipped; the old `... | last`
	selection picked #2894 and fingerprinted its unrelated diff."""
	out = _resolve(
		2872,
		head_prs=[],
		xref_prs=[2889, 2894],
		pr_json={
			2889: {
				"merged_at": "2026-05-22T08:05:59Z",
				"body": "fix(ai-memory): add jittered backoff.\n\nRefs #2872",
			},
			2894: {
				"merged_at": None,
				"body": "fix(stall-recovery): skip open-PR guard.\n\nRefs #2872",
			},
		},
	)
	assert out == "", f"Refs-only cross-references must not be captured; got {out!r}"


def test_refs_keyword_alone_is_not_a_closing_keyword():
	"""A merged PR whose body only says `Refs #N` (no close/fix/resolve)
	must not be treated as the implementation PR."""
	out = _resolve(
		500,
		head_prs=[],
		xref_prs=[3100],
		pr_json={3100: {"merged_at": "2026-05-01T00:00:00Z", "body": "Refs #500"}},
	)
	assert out == "", out


def test_embedded_keyword_suffix_is_not_treated_as_closing():
	"""Embedded suffixes like `autofixes #N` must not satisfy the
	closing-keyword matcher."""
	out = _resolve(
		500,
		head_prs=[],
		xref_prs=[3101],
		pr_json={3101: {"merged_at": "2026-05-01T00:00:00Z", "body": "Telemetry autofixes #500"}},
	)
	assert out == "", out


# ---------------------------------------------------------------------------
# Tier 2 — closing-keyword body match across merged cross-referenced PRs
# ---------------------------------------------------------------------------


def test_tier2_selects_merged_closing_keyword_pr():
	"""No `ai/issue-<n>` branch, but a merged cross-referenced PR closes
	the issue via `Closes #N` — select it."""
	out = _resolve(
		500,
		head_prs=[],
		xref_prs=[3001],
		pr_json={3001: {"merged_at": "2026-05-01T00:00:00Z", "body": "Closes #500"}},
	)
	assert out == "3001", out


def test_tier2_matches_issue_url_closing_form():
	"""The implement workflow emits `Closes <issue URL>`, not `Closes #N`.
	The URL form must be recognised."""
	out = _resolve(
		600,
		head_prs=[],
		xref_prs=[5001],
		pr_json={
			5001: {
				"merged_at": "2026-05-01T00:00:00Z",
				"body": "Automated implementation. Closes https://github.com/owner/repo/issues/600",
			}
		},
	)
	assert out == "5001", out


def test_tier2_accepts_fix_and_resolve_inflections():
	"""`Fixed`/`Resolves` are GitHub closing keywords too."""
	assert _resolve(
		700,
		xref_prs=[6001],
		pr_json={6001: {"merged_at": "2026-05-01T00:00:00Z", "body": "Fixed #700"}},
	) == "6001"
	assert _resolve(
		701,
		xref_prs=[6002],
		pr_json={6002: {"merged_at": "2026-05-01T00:00:00Z", "body": "Resolves #701"}},
	) == "6002"


def test_open_pr_with_closing_keyword_is_not_selected():
	"""An open PR cannot have contributed its diff to the integration
	branch, so it must not be captured even with a closing keyword."""
	out = _resolve(
		700,
		head_prs=[],
		xref_prs=[6001],
		pr_json={6001: {"merged_at": None, "body": "Closes #700"}},
	)
	assert out == "", out


def test_tier2_picks_newest_when_multiple_closing_prs():
	"""Multiple merged PRs closing the same issue — pin to the newest
	(highest-numbered) one deterministically."""
	out = _resolve(
		800,
		head_prs=[],
		xref_prs=[7001, 7050],
		pr_json={
			7001: {"merged_at": "2026-05-01T00:00:00Z", "body": "Closes #800"},
			7050: {"merged_at": "2026-05-10T00:00:00Z", "body": "Closes #800"},
		},
	)
	assert out == "7050", out


def test_word_boundary_rejects_superstring_issue_number():
	"""`Closes #28730` must NOT satisfy a lookup for issue #2873 — the
	digit run must end at a non-digit boundary."""
	out = _resolve(
		2873,
		head_prs=[],
		xref_prs=[4001],
		pr_json={4001: {"merged_at": "2026-05-01T00:00:00Z", "body": "Closes #28730"}},
	)
	assert out == "", f"#28730 must not match issue #2873; got {out!r}"


def test_tier1_wins_over_tier2():
	"""When both signals are present, the `ai/issue-<n>` branch PR is the
	stronger signal and must be preferred."""
	out = _resolve(
		900,
		head_prs=[{"number": 9000, "mergedAt": "2026-05-22T00:00:00Z"}],
		xref_prs=[9999],
		pr_json={9999: {"merged_at": "2026-05-22T01:00:00Z", "body": "Closes #900"}},
	)
	assert out == "9000", out


def test_invalid_issue_number_yields_empty():
	"""A non-numeric issue argument fails open to empty."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		script = textwrap.dedent(f"""
		set -uo pipefail
		{_STUBS}
		source helpers.sh
		_subissue_closing_pr_number "not-a-number"
		""")
		r = _run_bash(script, cwd=tmp)
		assert r.returncode == 0, r.stderr
		assert r.stdout.strip() == "", r.stdout


def test_no_cross_references_yields_empty():
	"""No `ai/issue-<n>` branch and no cross-referenced PRs at all — empty
	(capture is correctly skipped)."""
	out = _resolve(1000, head_prs=[], xref_prs=[])
	assert out == "", out


# ---------------------------------------------------------------------------
# Direct-invocation entrypoint — ci.yml runs each test as `python3 <file>`
# from an explicit allowlist (no pytest discovery).
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
		except Exception as e:  # noqa: BLE001 — test runner surfaces every failure
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
