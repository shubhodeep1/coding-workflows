#!/usr/bin/env python3
"""The dispatch watcher cancels a child run whose completion watch timed out (#9706).

Follow-up to #9532: in release run 43590943288 dispatched children stayed
queued for hours with no runner after their watch gave up. The workflow steps
in test-and-mark-stable.yml already cancel such a child; this covers the same
behaviour in scripts/dispatch_and_watch_workflow_run.sh itself. On the
completion-timeout path the watcher prints a diagnosis (queued the whole time
versus in_progress), cancels the run unless DISPATCH_WATCH_CANCEL_ON_TIMEOUT is
false, and still ends with the unchanged `::error::... timed out` line and
exit 1.
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

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the watcher")

CANCEL_CALL = "api -X POST repos/owner/repo/actions/runs/4242/cancel"
TIMEOUT_LINE = "::error::validate run #4242 timed out"
# Same extraction the workflow steps' timeout block uses.
WORKFLOW_SED_RE = re.compile(r"^::error::.* run #([0-9][0-9]*) timed out$")
QUEUED_DIAG = (
	"::warning::validate run #4242 watch diagnosis: status=queued the whole time "
	"(no runner was assigned); status_reads="
)
IN_PROGRESS_DIAG = (
	"::warning::validate run #4242 watch diagnosis: status=in_progress "
	"(a runner was assigned; the run was slow); status_reads="
)
UNKNOWN_DIAG = (
	"::warning::validate run #4242 watch diagnosis: last status=unknown "
	"(could not tell queued from in_progress); status_reads=0"
)

GH_STUB = """#!/usr/bin/env bash
echo "$*" >> "${STUB_GH_LOG}"
[ "${1:-}" = "workflow" ] && exit 0
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
	*"/runs?event=workflow_dispatch&per_page=1") body='{"workflow_runs":[]}' ;;
	*"/runs?event=workflow_dispatch&"*) body='{"workflow_runs":[{"id":4242,"created_at":"2026-11-15T01:26:16Z"}]}' ;;
	*"/actions/runs/4242/cancel")
		if [ "${STUB_CANCEL_FAIL:-0}" = "1" ]; then
			echo "HTTP 409: Cannot cancel a workflow run that is completed." >&2
			exit 1
		fi
		body='{}' ;;
	*"/actions/runs/4242")
		if [ "${STUB_STATUS_FAIL:-0}" = "1" ]; then
			echo "HTTP 404: Not Found" >&2
			exit 1
		fi
		if [ -n "${STUB_RUN_CONCLUSION:-}" ]; then
			body="{\\"status\\":\\"${STUB_RUN_STATUS}\\",\\"conclusion\\":\\"${STUB_RUN_CONCLUSION}\\"}"
		else
			body="{\\"status\\":\\"${STUB_RUN_STATUS}\\",\\"conclusion\\":null}"
		fi
		;;
	*) exit 1 ;;
esac
if [ -n "${jq_expr}" ]; then
	printf '%s' "${body}" | jq -r "${jq_expr}"
else
	printf '%s\\n' "${body}"
fi
"""


def _run_watcher(
	*,
	run_status: str = "queued",
	run_conclusion: str = "",
	cancel_switch: str | None = None,
	status_fail: bool = False,
	cancel_fail: bool = False,
) -> tuple[int, str, list[str], str]:
	bash = shutil.which("bash")
	assert bash is not None
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		(tmp / "scripts").mkdir()
		for helper in HELPERS:
			shutil.copyfile(REPO_ROOT / "scripts" / helper, tmp / "scripts" / helper)
		bindir = tmp / "bin"
		bindir.mkdir()
		gh_log = tmp / "gh.log"
		gh_log.write_text("", encoding="utf-8")
		gh = bindir / "gh"
		gh.write_text(GH_STUB, encoding="utf-8")
		gh.chmod(0o755)
		output = tmp / "github_output"
		output.write_text("", encoding="utf-8")
		env = {
			"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
			"HOME": str(tmp),
			"GITHUB_OUTPUT": str(output),
			"STUB_GH_LOG": str(gh_log),
			"STUB_RUN_STATUS": run_status,
			"STUB_RUN_CONCLUSION": run_conclusion,
			"STUB_STATUS_FAIL": "1" if status_fail else "0",
			"STUB_CANCEL_FAIL": "1" if cancel_fail else "0",
		}
		if cancel_switch is not None:
			env["DISPATCH_WATCH_CANCEL_ON_TIMEOUT"] = cancel_switch
		proc = subprocess.run(
			[
				bash,
				"scripts/dispatch_and_watch_workflow_run.sh",
				"--repo", "owner/repo",
				"--workflow", "internal-validate.yml",
				"--display-name", "validate",
				"--registration-timeout-secs", "30",
				"--registration-poll-interval-secs", "1",
				"--completion-timeout-secs", "2",
				"--completion-poll-interval-secs", "1",
			],
			cwd=tmp,
			env=env,
			capture_output=True,
			text=True,
			check=False,
			timeout=120,
		)
		calls = [line for line in gh_log.read_text(encoding="utf-8").splitlines() if line]
		github_output = output.read_text(encoding="utf-8")
	return proc.returncode, proc.stdout + proc.stderr, calls, github_output


def _cancels(calls: list[str]) -> list[str]:
	return [c for c in calls if "/cancel" in c]


def test_queued_whole_time_reports_diagnosis_cancels_and_fails() -> None:
	rc, out, calls, github_output = _run_watcher()
	assert rc == 1, out
	assert QUEUED_DIAG in out, out
	assert _cancels(calls) == [CANCEL_CALL], calls
	assert "Cancel requested for watched run #4242 (completion watch timeout)" in out, out
	lines = out.splitlines()
	assert lines[-1] == TIMEOUT_LINE, out
	diag_at = next(i for i, line in enumerate(lines) if line.startswith(QUEUED_DIAG))
	cancel_at = lines.index("Cancel requested for watched run #4242 (completion watch timeout)")
	assert diag_at < cancel_at < lines.index(TIMEOUT_LINE), out
	matches = [m.group(1) for m in (WORKFLOW_SED_RE.match(line) for line in lines) if m]
	assert matches and matches[-1] == "4242", out
	assert "dispatched_run_id=4242" in github_output.splitlines(), github_output
	assert not [line for line in github_output.splitlines() if line.startswith("run_id=")], github_output


def test_in_progress_reports_slow_run_and_cancels() -> None:
	rc, out, calls, _ = _run_watcher(run_status="in_progress")
	assert rc == 1, out
	assert IN_PROGRESS_DIAG in out, out
	assert QUEUED_DIAG not in out, out
	assert _cancels(calls) == [CANCEL_CALL], calls
	assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_switch_false_skips_the_cancel() -> None:
	for switch in ("false", "FALSE"):
		rc, out, calls, _ = _run_watcher(cancel_switch=switch)
		assert rc == 1, (switch, out)
		assert not _cancels(calls), (switch, calls)
		assert "Cancel of watched run #4242 skipped (DISPATCH_WATCH_CANCEL_ON_TIMEOUT=false)" in out, out
		assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_unrecognised_switch_skips_the_cancel_without_printing_the_value() -> None:
	for switch in ("off", "0"):
		rc, out, calls, _ = _run_watcher(cancel_switch=switch)
		assert rc == 1, (switch, out)
		assert not _cancels(calls), (switch, calls)
		warning = "::warning::DISPATCH_WATCH_CANCEL_ON_TIMEOUT is neither true nor false; treating it as false"
		assert warning in out.splitlines(), out
		assert "Cancel of watched run #4242 skipped (DISPATCH_WATCH_CANCEL_ON_TIMEOUT unrecognised)" in out, out
		assert "DISPATCH_WATCH_CANCEL_ON_TIMEOUT=false" not in out, out
		assert f"={switch}" not in out and f"'{switch}'" not in out, out


def test_true_or_empty_switch_cancels() -> None:
	for switch in ("", "TRUE", "true"):
		rc, out, calls, _ = _run_watcher(cancel_switch=switch)
		assert rc == 1, (switch, out)
		assert _cancels(calls) == [CANCEL_CALL], (switch, calls)
		assert "neither true nor false" not in out, out


def test_failed_cancel_only_warns_and_still_fails() -> None:
	rc, out, calls, _ = _run_watcher(cancel_fail=True)
	assert rc == 1, out
	assert _cancels(calls) == [CANCEL_CALL], calls
	assert "::warning::Could not cancel watched run #4242 after the completion watch timeout" in out, out
	assert "HTTP 409" not in out, out
	assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_failed_status_reads_report_unknown_and_still_cancel() -> None:
	rc, out, calls, _ = _run_watcher(status_fail=True)
	assert rc == 1, out
	assert UNKNOWN_DIAG in out, out
	assert _cancels(calls) == [CANCEL_CALL], calls
	assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_successful_completion_makes_no_cancel_and_no_diagnosis() -> None:
	rc, out, calls, github_output = _run_watcher(run_status="completed", run_conclusion="success")
	assert rc == 0, out
	assert not _cancels(calls), calls
	assert "watch diagnosis" not in out, out
	assert "run_id=4242" in github_output.splitlines(), github_output


def test_failed_completion_makes_no_cancel() -> None:
	rc, out, calls, _ = _run_watcher(run_status="completed", run_conclusion="failure")
	assert rc == 1, out
	assert "::error::validate run #4242 concluded failure" in out, out
	assert not _cancels(calls), calls
	assert "watch diagnosis" not in out, out
	assert "timed out" not in out, out


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
