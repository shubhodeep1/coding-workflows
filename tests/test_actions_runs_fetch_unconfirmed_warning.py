#!/usr/bin/env python3
"""Runtime tests for the _load_actions_runs_cached fetch-failure diagnostic.

Background (RC-3 of the consumer-repo #13/#14 stall-recovery incident): when the
poller's per-tick actions-runs fetch fails (auth 403, transient API error) or
returns an unparseable body, ``_load_actions_runs_cached`` fails open to an empty
blob (``{"workflow_runs":[]}``) so non-destructive callers (``build_active_issue_set``)
keep working. Before this fix it failed open *silently*, so an investigation could
not tell a genuine "zero in-flight runs" from "the fetch never succeeded" — the
exact ambiguity that left ``Active issue set is empty (cache: total=0 ...)``
unattributable (perms vs poisoned/empty TTL cache vs transient).

The fix:

1. Corrects the exit-code capture from the buggy ``if ! cmd; then api_rc=$?``
   idiom (which records the negated condition's status, always 0) to
   ``cmd || api_rc=$?`` so ``api_rc`` reflects the real failure code.
2. Emits one structured ``::warning::rate_limit_audit_fallback ...
   reason=fetch_unconfirmed`` on the fail-open path (carrying ``api_rc``, the
   HTTP ``status`` line, ``cache_hit``, and the first stderr line) and emits
   NOTHING on the genuine-empty SUCCESS path, restoring the distinction.

Same function-extraction-plus-stub pattern as
``test_retrigger_inflight_direct_fallback.py``: pull the helper definition out of
the production script with awk, source it, and drive it with a controlled
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

WARN_TOKEN = "rate_limit_audit_fallback"
WARN_REASON = "reason=fetch_unconfirmed"
EMPTY_BLOB = '{"workflow_runs":[]}'


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


# Extract a single shell function body from POLLER_SCRIPT: match the declaration
# line (``fn()`` at column 0) through the first closing ``}`` at column 0.
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


# ``gh`` (the conditional ``gh api -i`` fetch) emits GH_STDOUT to stdout and
# GH_STDERR to stderr and exits GH_RC, so a single env triple drives every case.
# ``_safe_gh_jq`` (the queued/completed secondary listings on the success path)
# returns an empty array. ``gh_retry`` is a passthrough so the helper sees the
# stub's real exit code (the point of the api_rc-capture regression test).
_STUBS = r"""
gh_retry() { "$@"; }
_safe_gh_jq() { printf '%s' '[]'; }
gh() {
	if [ -n "${GH_STDOUT:-}" ]; then printf '%s' "${GH_STDOUT}"; fi
	if [ -n "${GH_STDERR:-}" ]; then printf '%s\n' "${GH_STDERR}" >&2; fi
	return "${GH_RC:-0}"
}
export -f gh gh_retry _safe_gh_jq 2>/dev/null || true
"""


def _bootstrap(tmp_dir: Path) -> Path:
	"""Write ``helpers.sh`` with the production helper, return its path."""
	extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
	script = textwrap.dedent(f"""
	set -euo pipefail
	{extractor}
	: > helpers.sh
	extract_fn '_load_actions_runs_cached' >> helpers.sh
	""")
	r = _run_bash(script, cwd=tmp_dir)
	assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
	helpers = tmp_dir / "helpers.sh"
	assert helpers.exists() and helpers.stat().st_size > 0, "helpers.sh not produced"
	body = helpers.read_text(encoding="utf-8")
	assert "_load_actions_runs_cached()" in body, "missing helper in helpers.sh"
	return helpers


def _invoke(
	gh_stdout: str = "",
	gh_stderr: str = "",
	gh_rc: int = 0,
	cache_ttl: str = "300",
) -> subprocess.CompletedProcess:
	"""Source the extracted helper and run it with the given gh stub triple.

	Returns the CompletedProcess: ``.stdout`` is the helper's blob, ``.stderr``
	is the helper's diagnostic stream (the gh stub's own stderr is captured into
	the helper's response_err by its ``2>`` redirect, so it never leaks here).
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		env = {
			"GH_STDOUT": gh_stdout,
			"GH_STDERR": gh_stderr,
			"GH_RC": str(gh_rc),
			"ACTIONS_RUNS_CACHE_TTL_SECONDS": cache_ttl,
		}
		body = textwrap.dedent(f"""
		set -uo pipefail
		_ACTIONS_RUNS_BLOB_READY=''
		_ACTIONS_RUNS_BLOB_CACHE=''
		{_STUBS}
		source helpers.sh
		_load_actions_runs_cached
		""")
		r = _run_bash(body, cwd=tmp, env=env)
		assert r.returncode == 0, f"helper exited {r.returncode}: {r.stderr}"
		return r


# ---------------------------------------------------------------------------
# Fail-open path emits the diagnostic
# ---------------------------------------------------------------------------

def test_transient_fetch_failure_warns_and_fails_open_to_empty():
	"""A transient fetch failure (non-zero exit, no body) must emit the
	fetch_unconfirmed warning and still fail open to an empty blob."""
	r = _invoke(gh_stdout="", gh_stderr="error connecting to api.github.com", gh_rc=1)
	assert r.stdout.strip() == EMPTY_BLOB, f"expected empty blob, got: {r.stdout!r}"
	assert WARN_TOKEN in r.stderr and WARN_REASON in r.stderr, f"missing warning: {r.stderr!r}"
	assert "cache_hit=false" in r.stderr, r.stderr
	assert "err='error connecting to api.github.com'" in r.stderr, r.stderr


def test_403_failure_captures_http_status_line():
	"""A perms failure surfaces the HTTP status line so the next investigation
	can tell 403 (PAT lacks actions:read) from a transient blip — the open
	question the silent fail-open made unanswerable. The trailing CRLF CR must
	be stripped so the single-line warning is not garbled."""
	r = _invoke(gh_stdout="HTTP/2 403\r\n\r\n", gh_stderr="gh: Resource not accessible by integration (HTTP 403)", gh_rc=1)
	assert r.stdout.strip() == EMPTY_BLOB
	assert WARN_REASON in r.stderr, r.stderr
	assert "status='HTTP/2 403'" in r.stderr, f"status line not surfaced/sanitized: {r.stderr!r}"
	assert "\r" not in r.stderr, "carriage return leaked into the warning line"


def test_warning_reports_the_real_exit_code():
	"""Regression guard for the api_rc-capture fix: with the old
	`if ! cmd; then api_rc=$?` idiom this is always api_rc=0; the corrected
	`cmd || api_rc=$?` reports the stub's real code."""
	r = _invoke(gh_stdout="", gh_stderr="boom", gh_rc=22)
	assert WARN_REASON in r.stderr, r.stderr
	assert "api_rc=22" in r.stderr, f"real exit code not captured: {r.stderr!r}"


# ---------------------------------------------------------------------------
# Genuine-empty SUCCESS path stays silent (the diagnostic separation)
# ---------------------------------------------------------------------------

def test_genuine_empty_success_does_not_warn():
	"""A confirmed fetch that legitimately returns zero in-flight runs must NOT
	emit the warning — that is the whole point of RC-3: separate a real empty
	from a failed fetch."""
	r = _invoke(gh_stdout=EMPTY_BLOB, gh_stderr="", gh_rc=0)
	assert r.stdout.strip() == EMPTY_BLOB, f"expected empty blob, got: {r.stdout!r}"
	assert WARN_REASON not in r.stderr, f"genuine-empty success wrongly warned: {r.stderr!r}"
	assert WARN_TOKEN not in r.stderr, f"unexpected fallback warning on success: {r.stderr!r}"


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
