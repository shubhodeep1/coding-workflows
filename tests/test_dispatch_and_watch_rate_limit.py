#!/usr/bin/env python3
"""The dispatch watcher waits out GitHub rate limits on `gh workflow run` (#10197).

Release gate run 45927857867 failed its orphan-workflows-test job because one
`gh workflow run update_workflows.yml` hit the PAT's primary REST rate limit
(HTTP 403 at 03:14:38). scripts/dispatch_and_watch_workflow_run.sh retried 5
times in 32 seconds and gave up; the limit reset at 03:23:03. A rate-limited
dispatch now waits (60 s on a secondary limit, until the core reset on a
primary one), bounded by DISPATCH_RATE_LIMIT_MAX_WAIT_SECS (default 1500, 0 =
off), without using up one of the dispatch attempts. Each wait logs one
`DISPATCH_RATE_LIMIT_WAIT` line; a wait past the cap logs
`outcome=cap_reached` and the existing `failed after N attempts` error line.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPERS = ("dispatch_and_watch_workflow_run.sh", "comprehensive_test_and_release_gh_api.sh")
WATCHER = REPO_ROOT / "scripts" / "dispatch_and_watch_workflow_run.sh"
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the watcher")

PRIMARY_ERR = "HTTP 403: API rate limit exceeded for user ID 11442166. (https://api.github.com/repos/owner/repo/actions/workflows/update_workflows.yml/dispatches)"
SECONDARY_ERR = "HTTP 403: You have exceeded a secondary rate limit. Please wait a few minutes before you try again."
SERVER_ERR = "HTTP 500: Internal Server Error (https://api.github.com/repos/owner/repo/actions/workflows/update_workflows.yml/dispatches)"
FAILED_LINE = "::error::gh workflow run update_workflows.yml failed after 5 attempts"
WAIT_RE = re.compile(
	r"^DISPATCH_RATE_LIMIT_WAIT workflow=update_workflows\.yml kind=(primary|secondary) "
	r"wait_s=([0-9]+) waited_total_s=([0-9]+) max_s=([0-9]+)( outcome=cap_reached)?$"
)

GH_STUB = r"""#!/usr/bin/env bash
echo "$*" >> "${STUB_GH_LOG}"
if [ "${1:-}" = "workflow" ]; then
	n="$(cat "${STUB_DISPATCH_COUNT}" 2>/dev/null || echo 0)"
	n=$((n + 1))
	echo "${n}" > "${STUB_DISPATCH_COUNT}"
	if [ "${n}" -le "${STUB_DISPATCH_FAILS:-0}" ]; then
		printf '%s\n' "${STUB_DISPATCH_ERR}" >&2
		exit 1
	fi
	exit 0
fi
shift
jq_expr=""
path=""
while [ "$#" -gt 0 ]; do
	case "$1" in
		--jq) jq_expr="$2"; shift 2 ;;
		-X) shift 2 ;;
		*) path="$1"; shift ;;
	esac
done
case "${path}" in
	rate_limit)
		[ "${STUB_RATE_LIMIT_FAIL:-0}" = "1" ] && exit 1
		reset=$(( $(date +%s) + ${STUB_RESET_OFFSET:-505} ))
		body="{\"resources\":{\"core\":{\"limit\":5000,\"remaining\":${STUB_REMAINING:-0},\"reset\":${reset}}}}" ;;
	*"/runs?event=workflow_dispatch&per_page=1") body='{"workflow_runs":[]}' ;;
	*"/runs?event=workflow_dispatch&"*) body='{"workflow_runs":[{"id":4242,"created_at":"2026-11-27T03:23:10Z"}]}' ;;
	*"/actions/runs/4242") body='{"status":"completed","conclusion":"success"}' ;;
	*) exit 1 ;;
esac
if [ -n "${jq_expr}" ]; then
	printf '%s' "${body}" | jq -r "${jq_expr}"
else
	printf '%s\n' "${body}"
fi
"""

SLEEP_STUB = """#!/usr/bin/env bash
echo "$1" >> "${STUB_SLEEP_LOG}"
"""


def _run(
	*,
	dispatch_err: str = PRIMARY_ERR,
	dispatch_fails: int = 0,
	reset_offset: int = 505,
	remaining: int = 0,
	rate_limit_fail: bool = False,
	cap: str | None = None,
) -> tuple[int, str, list[str], list[int], int]:
	bash = shutil.which("bash")
	assert bash is not None
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		(tmp / "scripts").mkdir()
		for helper in HELPERS:
			shutil.copyfile(REPO_ROOT / "scripts" / helper, tmp / "scripts" / helper)
		bindir = tmp / "bin"
		bindir.mkdir()
		for name, body in (("gh", GH_STUB), ("sleep", SLEEP_STUB)):
			stub = bindir / name
			stub.write_text(body, encoding="utf-8")
			stub.chmod(0o755)
		gh_log = tmp / "gh.log"
		gh_log.write_text("", encoding="utf-8")
		sleep_log = tmp / "sleep.log"
		sleep_log.write_text("", encoding="utf-8")
		count = tmp / "dispatch_count"
		output = tmp / "github_output"
		output.write_text("", encoding="utf-8")
		env = {
			"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
			"HOME": str(tmp),
			"GITHUB_OUTPUT": str(output),
			"STUB_GH_LOG": str(gh_log),
			"STUB_SLEEP_LOG": str(sleep_log),
			"STUB_DISPATCH_COUNT": str(count),
			"STUB_DISPATCH_FAILS": str(dispatch_fails),
			"STUB_DISPATCH_ERR": dispatch_err,
			"STUB_RESET_OFFSET": str(reset_offset),
			"STUB_REMAINING": str(remaining),
			"STUB_RATE_LIMIT_FAIL": "1" if rate_limit_fail else "0",
		}
		if cap is not None:
			env["DISPATCH_RATE_LIMIT_MAX_WAIT_SECS"] = cap
		proc = subprocess.run(
			[
				bash,
				"scripts/dispatch_and_watch_workflow_run.sh",
				"--repo", "owner/repo",
				"--workflow", "update_workflows.yml",
				"--display-name", "update_workflows",
				"--registration-timeout-secs", "600",
				"--registration-poll-interval-secs", "5",
				"--completion-timeout-secs", "600",
				"--completion-poll-interval-secs", "15",
				"--dispatch-max-attempts", "5",
				"--dispatch-backoff-base-secs", "2",
			],
			cwd=tmp,
			env=env,
			capture_output=True,
			text=True,
			check=False,
			timeout=120,
		)
		calls = [line for line in gh_log.read_text(encoding="utf-8").splitlines() if line]
		sleeps = [int(line) for line in sleep_log.read_text(encoding="utf-8").splitlines() if line.strip()]
		dispatches = int(count.read_text(encoding="utf-8").strip()) if count.exists() else 0
	return proc.returncode, proc.stdout + proc.stderr, calls, sleeps, dispatches


def _waits(out: str) -> list[re.Match[str]]:
	return [m for m in (WAIT_RE.match(line) for line in out.splitlines()) if m]


def _rate_limit_reads(calls: list[str]) -> list[str]:
	return [c for c in calls if c.startswith("api rate_limit")]


def test_primary_rate_limit_replay_waits_for_reset_then_dispatches() -> None:
	# The 03:14:38 403 with the reset at 03:23:03 (505 s later).
	rc, out, calls, sleeps, dispatches = _run(dispatch_fails=1, reset_offset=505)
	assert rc == 0, out
	waits = _waits(out)
	assert len(waits) == 1, out
	kind, wait_s, total, max_s, cap_reached = waits[0].groups()
	assert kind == "primary" and cap_reached is None, out
	assert 509 <= int(wait_s) <= 511, out
	assert int(total) == int(wait_s) and max_s == "1500", out
	assert sleeps == [int(wait_s)], sleeps
	assert dispatches == 2, out
	assert len(_rate_limit_reads(calls)) == 1, calls
	assert "(attempt 1/5)" not in out, out
	# gh's own error text is still printed.
	assert PRIMARY_ERR in out, out


def test_two_primary_failures_then_success_keep_the_attempt_counter() -> None:
	rc, out, calls, sleeps, dispatches = _run(dispatch_fails=2, reset_offset=300)
	assert rc == 0, out
	waits = _waits(out)
	assert [w.group(1) for w in waits] == ["primary", "primary"], out
	assert all(304 <= int(w.group(2)) <= 306 for w in waits), out
	assert int(waits[1].group(3)) == int(waits[0].group(2)) + int(waits[1].group(2)), out
	assert sleeps == [int(w.group(2)) for w in waits], sleeps
	assert dispatches == 3, out
	assert len(_rate_limit_reads(calls)) == 2, calls
	assert "::warning::gh workflow run" not in out, out


def test_secondary_rate_limit_waits_sixty_seconds_without_a_rate_limit_read() -> None:
	rc, out, calls, sleeps, dispatches = _run(dispatch_err=SECONDARY_ERR, dispatch_fails=1)
	assert rc == 0, out
	waits = _waits(out)
	assert [(w.group(1), w.group(2), w.group(3)) for w in waits] == [("secondary", "60", "60")], out
	assert sleeps == [60], sleeps
	assert dispatches == 2, out
	assert not _rate_limit_reads(calls), calls


def test_reset_beyond_cap_fails_at_once_with_the_existing_error_line() -> None:
	rc, out, calls, sleeps, dispatches = _run(dispatch_fails=99, reset_offset=900, cap="300")
	assert rc == 1, out
	waits = _waits(out)
	assert len(waits) == 1, out
	kind, wait_s, total, max_s, cap_reached = waits[0].groups()
	assert kind == "primary" and cap_reached == " outcome=cap_reached", out
	assert int(wait_s) >= 904 and total == "0" and max_s == "300", out
	assert FAILED_LINE in out.splitlines(), out
	assert sleeps == [], sleeps
	assert dispatches == 1, out


def test_wait_that_outlasts_the_remaining_cap_reports_cap_reached() -> None:
	rc, out, _, sleeps, dispatches = _run(dispatch_err=SECONDARY_ERR, dispatch_fails=99, cap="150")
	assert rc == 1, out
	waits = _waits(out)
	assert [(w.group(3), w.group(5)) for w in waits] == [("60", None), ("120", None), ("120", " outcome=cap_reached")], out
	assert sleeps == [60, 60], sleeps
	assert dispatches == 3, out
	assert FAILED_LINE in out.splitlines(), out


def test_non_rate_limit_failure_keeps_todays_backoff() -> None:
	rc, out, calls, sleeps, dispatches = _run(dispatch_err=SERVER_ERR, dispatch_fails=99)
	assert rc == 1, out
	assert sleeps == [2, 4, 8, 16], sleeps
	assert dispatches == 5, out
	for attempt, backoff in ((1, 2), (2, 4), (3, 8), (4, 16)):
		assert (
			f"::warning::gh workflow run update_workflows.yml failed (attempt {attempt}/5), retrying in {backoff}s..."
			in out
		), out
	assert FAILED_LINE in out.splitlines(), out
	assert not _waits(out), out
	assert not _rate_limit_reads(calls), calls


def test_cap_zero_keeps_todays_behaviour_on_a_rate_limit() -> None:
	rc, out, calls, sleeps, dispatches = _run(dispatch_fails=99, cap="0")
	assert rc == 1, out
	assert sleeps == [2, 4, 8, 16], sleeps
	assert dispatches == 5, out
	assert FAILED_LINE in out.splitlines(), out
	assert "DISPATCH_RATE_LIMIT_WAIT" not in out, out
	assert not _rate_limit_reads(calls), calls


def test_invalid_cap_warns_and_uses_the_default() -> None:
	rc, out, _, _, _ = _run(dispatch_fails=1, cap="abc")
	assert rc == 0, out
	assert "::warning::DISPATCH_RATE_LIMIT_MAX_WAIT_SECS must be an integer (0-99999); using 1500" in out, out
	waits = _waits(out)
	assert len(waits) == 1 and waits[0].group(4) == "1500", out


def test_failed_rate_limit_read_waits_one_minute() -> None:
	rc, out, calls, sleeps, _ = _run(dispatch_fails=1, rate_limit_fail=True)
	assert rc == 0, out
	waits = _waits(out)
	assert [(w.group(1), w.group(2)) for w in waits] == [("primary", "60")], out
	assert sleeps == [60], sleeps
	assert len(_rate_limit_reads(calls)) == 1, calls


def test_core_quota_left_is_treated_as_secondary() -> None:
	rc, out, calls, sleeps, _ = _run(dispatch_err="HTTP 429: Too Many Requests", dispatch_fails=1, remaining=42)
	assert rc == 0, out
	waits = _waits(out)
	assert [(w.group(1), w.group(2)) for w in waits] == [("secondary", "60")], out
	assert sleeps == [60], sleeps
	assert len(_rate_limit_reads(calls)) == 1, calls


def test_local_regex_matches_gh_helpers_is_gh_rate_limit() -> None:
	helpers = GH_HELPERS.read_text(encoding="utf-8")
	body = re.search(r"^_is_gh_rate_limit\(\)\n\{\n(.*?)\n\}", helpers, re.M | re.S)
	assert body is not None
	shared = re.search(r"grep -qiE '([^']+)'", body.group(1))
	assert shared is not None
	local = re.search(r"^DISPATCH_WATCH_RATE_LIMIT_RE='([^']+)'$", WATCHER.read_text(encoding="utf-8"), re.M)
	assert local is not None
	assert local.group(1) == shared.group(1)


def _workflow_steps(text: str) -> list[tuple[str, str]]:
	chunks = re.split(r"\n(?=      - name: )", text)[1:]
	steps = []
	for chunk in chunks:
		name = chunk.split("\n", 1)[0][len("      - name: "):].strip().strip("\"'")
		steps.append((name, chunk))
	return steps


def test_gate_dispatch_steps_fit_rate_limit_cap_in_step_budget() -> None:
	"""cap + watch deadline + 4 s pre-sleep + 60 s margin fits each step budget."""
	text = WORKFLOW.read_text(encoding="utf-8")
	checked = []
	orchestrate_cap = None
	for name, chunk in _workflow_steps(text):
		if "bash ./scripts/dispatch_and_watch_workflow_run.sh \\\n" not in chunk or "--workflow" not in chunk:
			continue
		header = chunk.split("        run: |", 1)[0]
		cap_match = re.search(r'^          DISPATCH_RATE_LIMIT_MAX_WAIT_SECS: "([0-9]+)"$', header, re.M)
		cap = int(cap_match.group(1)) if cap_match else 1500
		if name == "Dispatch internal-orchestrate.yml with multi-issue project":
			orchestrate_cap = cap_match.group(1) if cap_match else None
		budget = re.search(r"^        timeout-minutes: ([0-9]+)$", header, re.M)
		if budget is None:
			continue
		deadlines = [
			int(v) for v in re.findall(r"--(?:completion|registration)-timeout-secs ([0-9]+)", chunk)
		]
		assert deadlines, name
		assert cap + max(deadlines) + 64 <= int(budget.group(1)) * 60, (name, cap, max(deadlines), budget.group(1))
		checked.append(name)
	assert "Dispatch & watch — update_workflows (allow_workflow_edits=false)" in checked, checked
	assert len(checked) >= 5, checked
	assert orchestrate_cap is not None, "the orchestrate dispatch step must set an explicit cap"
