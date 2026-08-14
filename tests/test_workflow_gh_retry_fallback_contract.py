#!/usr/bin/env python3
"""Contract tests for workflow-local gh_retry fallback shims."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPREHENSIVE_RELEASE_GH_API_HELPER = REPO_ROOT / "scripts" / "comprehensive_test_and_release_gh_api.sh"
DISPATCH_WATCH_HELPER = REPO_ROOT / "scripts" / "dispatch_and_watch_workflow_run.sh"
SOURCE_LINE = 'source scripts/gh_helpers.sh 2>/dev/null || true'
FALLBACK_LINE = 'type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }'
SAFE_GH_JQ_LINE = 'type _safe_gh_jq >/dev/null 2>&1 || _safe_gh_jq() {'
TARGET_STEPS = (
	(".github/workflows/clarify.yml", "Fetch issue metadata"),
	(".github/workflows/clarify.yml", "Fetch issue comments"),
	(".github/workflows/clarify.yml", "Post clarification questions"),
	(".github/workflows/clarify.yml", "Comment on issue failure"),
	(".github/workflows/cancel_on_pr_close.yml", "Cancel queued/in-progress runs for closed PR branch"),
	(".github/workflows/mark-stable.yml", "Verify CI passed on stable"),
	(".github/workflows/mark-stable.yml", "Notify consumer repos via repository_dispatch"),
	(".github/workflows/orchestrate_poll.yml", "Find active tracking issues"),
	(".github/workflows/plan.yml", "Fetch issue metadata"),
	(".github/workflows/plan.yml", "Fetch issue comments"),
	(".github/workflows/plan.yml", "Skip when issue already has a PR"),
	(".github/workflows/plan.yml", "Comment on issue failure"),
	(".github/workflows/orchestrate.yml", "Create tracking issue"),
	(".github/workflows/orchestrate_clarify_respond.yml", "Fetch issue and tracking context"),
	(".github/workflows/orchestrate_clarify_respond.yml", "Comment on issue failure"),
	(".github/workflows/test-and-mark-stable.yml", "Verify CI passed on source branch"),
)
SAFE_FETCH_STEPS = (
	(".github/workflows/orchestrate.yml", "Create integration branch"),
)
BOOTSTRAPPED_GH_HELPER_WORKFLOWS = (
	".github/workflows/cancel_on_pr_close.yml",
	".github/workflows/orchestrate_poll.yml",
)


def _write_executable(path: Path, content: str) -> None:
	path.write_text(content, encoding="utf-8")
	path.chmod(0o755)


def _run_comprehensive_release_helper(
	*,
	gh_script: str,
	shell_body: str,
	sleep_script: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
	with tempfile.TemporaryDirectory() as tempdir_name:
		tempdir = Path(tempdir_name)
		bin_dir = tempdir / "bin"
		count_file = tempdir / "gh-count.txt"
		sleep_log_file = tempdir / "sleep.log"
		bin_dir.mkdir()
		_write_executable(bin_dir / "gh", gh_script)
		_write_executable(
			bin_dir / "sleep",
			sleep_script
			or "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"${TEST_SLEEP_LOG_FILE}\"\n",
		)
		env = os.environ.copy()
		env.update(
			{
				"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
				"TEST_RELEASE_HELPER_PATH": str(COMPREHENSIVE_RELEASE_GH_API_HELPER),
				"TEST_GH_COUNT_FILE": str(count_file),
				"TEST_SLEEP_LOG_FILE": str(sleep_log_file),
			}
		)
		command = textwrap.dedent(
			f"""\
			source \"${{TEST_RELEASE_HELPER_PATH}}\"
			{shell_body}
			"""
		)
		result = subprocess.run(
			["bash", "-lc", command],
			capture_output=True,
			check=False,
			cwd=REPO_ROOT,
			env=env,
			text=True,
		)
		gh_count = count_file.read_text(encoding="utf-8") if count_file.exists() else ""
		sleep_log = sleep_log_file.read_text(encoding="utf-8") if sleep_log_file.exists() else ""
		return result, gh_count, sleep_log


def _workflow_text(relative_path: str) -> str:
	return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _dispatch_watch_helper_text() -> str:
	return DISPATCH_WATCH_HELPER.read_text(encoding="utf-8")


def _step_block(text: str, step_name: str) -> str:
	marker = f"- name: {step_name}"
	start = text.find(marker)
	assert start != -1, f"Missing workflow step: {step_name}"
	next_step = text.find("\n      - name:", start + len(marker))
	if next_step == -1:
		return text[start:]
	return text[start:next_step]


def _step_lines(relative_path: str, step_name: str) -> list[str]:
	return _step_block(_workflow_text(relative_path), step_name).splitlines()


def test_targeted_optional_source_blocks_define_local_gh_retry_fallback() -> None:
	for relative_path, step_name in TARGET_STEPS:
		lines = _step_lines(relative_path, step_name)
		source_idx = next((i for i, line in enumerate(lines) if SOURCE_LINE in line), -1)
		fallback_idx = next((i for i, line in enumerate(lines) if FALLBACK_LINE in line), -1)
		call_idx = next(
			(
				i
				for i, line in enumerate(lines)
				if "gh_retry" in line and FALLBACK_LINE not in line
			),
			-1,
		)

		assert source_idx != -1, f"{relative_path} :: {step_name} must keep optional gh_helpers sourcing"
		assert fallback_idx != -1, f"{relative_path} :: {step_name} must define the gh_retry fallback shim"
		assert fallback_idx == source_idx + 1, (
			f"{relative_path} :: {step_name} must place the gh_retry fallback immediately after the optional source"
		)
		assert call_idx != -1, f"{relative_path} :: {step_name} must still call gh_retry"
		assert fallback_idx < call_idx, (
			f"{relative_path} :: {step_name} must define the fallback before its first gh_retry call"
		)


def test_targeted_gh_retry_only_blocks_do_not_define_safe_gh_jq() -> None:
	for relative_path, step_name in TARGET_STEPS:
		block = _step_block(_workflow_text(relative_path), step_name)
		assert SAFE_GH_JQ_LINE not in block, (
			f"{relative_path} :: {step_name} should stay gh_retry-only and not add an unused _safe_gh_jq shim"
		)


def test_safe_fetch_steps_define_canonical_safe_gh_jq_fallback() -> None:
	for relative_path, step_name in SAFE_FETCH_STEPS:
		block = _step_block(_workflow_text(relative_path), step_name)

		assert SOURCE_LINE in block
		assert FALLBACK_LINE in block
		assert SAFE_GH_JQ_LINE in block
		assert 'if ! _tmpf=$(mktemp "${TMPDIR:-/tmp}/_safe_gh_jq.XXXXXX" 2>/dev/null); then' in block
		assert 'echo "::error::_safe_gh_jq: failed to create temp file (mktemp failed); aborting without running: $*" >&2' in block
		assert 'if gh api "$@" > "${_tmpf}"; then' in block
		assert 'type _safe_gh_jq >/dev/null 2>&1 || _safe_gh_jq() { gh api "$@"; }' not in block
		assert 'if gh api "$@" > "${_tmpf}" 2>/dev/null; then' not in block
		assert (
			'DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${{ github.repository }}" --jq \'.default_branch\')"'
		) in block
		assert (
			'BASE_SHA="$(gh_retry _safe_gh_jq "repos/${{ github.repository }}/git/ref/heads/${DEFAULT_BRANCH_REF}" --jq \'.object.sha\')"'
		) in block


def test_bootstrapped_gh_retry_workflows_require_staged_helper_with_main_fallback() -> None:
	for relative_path in BOOTSTRAPPED_GH_HELPER_WORKFLOWS:
		text = _workflow_text(relative_path)
		fallback_step = (
			"Checkout workflow support source fallback for gh retry"
			if relative_path.endswith("orchestrate_poll.yml")
			else "Checkout workflow support source fallback"
		)
		fallback_block = _step_block(text, fallback_step)
		assert "path: .codex-workflow-src" in text, (
			f"{relative_path} must stage the workflow support checkout before sourcing gh_helpers.sh"
		)
		assert "continue-on-error: true" in fallback_block, (
			f"{relative_path} must keep the fallback checkout non-fatal until the explicit ensure step runs"
		)
		assert "if [ ! -d .codex-workflow-src ]; then" in text, (
			f"{relative_path} must fail if the workflow support checkout is unavailable"
		)
		assert "path: .codex-workflow-src-main" in text, (
			f"{relative_path} must keep the main-snapshot fallback for gh_helpers.sh staging"
		)
		assert 'src=".codex-workflow-src/scripts/gh_helpers.sh"' in text, (
			f"{relative_path} must stage gh_helpers.sh from the workflow support checkout"
		)
		assert 'if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/scripts/gh_helpers.sh" ]; then' in text, (
			f"{relative_path} must fall back to the main snapshot when the primary gh_helpers.sh is absent"
		)
		assert '::error::Missing required support script gh_helpers.sh' in text, (
			f"{relative_path} must hard-fail when gh_helpers.sh cannot be staged"
		)


def test_dispatch_watcher_registration_poll_prefers_id_delta_and_uses_created_window_only_as_fallback() -> None:
	text = _dispatch_watch_helper_text()
	baseline_flag = 'PRE_RUN_ID_LOOKUP_OK="false"'
	baseline_query = 'runs?event=workflow_dispatch&per_page=1'
	normal_query = 'runs?event=workflow_dispatch&per_page=10'
	normal_filter = '[.workflow_runs[] | select(.id > ${PRE_RUN_ID})] | sort_by(.created_at) | last | .id // empty'
	fallback_epoch = 'REGISTRATION_WINDOW_FALLBACK_EPOCH="$(( REGISTRATION_WINDOW_START_EPOCH - 1 ))"'
	fallback_query = 'runs?event=workflow_dispatch&created=>${REGISTRATION_WINDOW_FALLBACK_UTC}&per_page=10'
	legacy_query = 'runs?event=workflow_dispatch&created=>${REGISTRATION_WINDOW_START_UTC}&per_page=10'
	lookup_branch = 'if [ "${PRE_RUN_ID_LOOKUP_OK}" = "true" ]; then'
	dispatch_call = 'if ! dispatch_workflow; then'

	assert baseline_flag in text and baseline_query in text, (
		"scripts/dispatch_and_watch_workflow_run.sh must track whether the baseline PRE_RUN_ID snapshot succeeded "
		"before it chooses the registration lookup path."
	)
	assert normal_query in text and normal_filter in text and lookup_branch in text, (
		"scripts/dispatch_and_watch_workflow_run.sh must use the id > PRE_RUN_ID registration query on the normal path."
	)
	assert fallback_epoch in text and fallback_query in text, (
		"scripts/dispatch_and_watch_workflow_run.sh must keep a one-second-earlier created-window fallback for degraded PRE_RUN_ID lookups."
	)
	assert legacy_query not in text, (
		"scripts/dispatch_and_watch_workflow_run.sh must not apply the strict created-window filter on the normal registration path."
	)
	assert text.index(fallback_epoch) < text.index(dispatch_call) < text.index(fallback_query), (
		"scripts/dispatch_and_watch_workflow_run.sh must capture the fallback registration window before dispatching and only use it in the degraded path."
	)


def test_comprehensive_release_helper_retries_rate_limit_once_and_returns_retry_output() -> None:
	gh_script = textwrap.dedent(
		"""\
		#!/usr/bin/env bash
		count="$(cat \"${TEST_GH_COUNT_FILE}\" 2>/dev/null || echo 0)"
		count=$((count + 1))
		printf '%s' "${count}" > "${TEST_GH_COUNT_FILE}"
		if [ "${count}" -eq 1 ]; then
			echo "secondary rate limit" >&2
			exit 1
		fi
		printf 'success payload'
		"""
	)
	result, gh_count, sleep_log = _run_comprehensive_release_helper(
		gh_script=gh_script,
		shell_body=textwrap.dedent(
			"""\
			gh_api_safe "repos/example/repo"
			status=$?
			printf 'status=%s\n' "${status}"
			printf 'output=%s\n' "${GH_API_SAFE_OUTPUT}"
			printf 'backoff=%s\n' "${RATE_LIMIT_BACKOFF}"
			exit "${status}"
			"""
		),
	)

	assert result.returncode == 0
	assert result.stdout == "status=0\noutput=success payload\nbackoff=0\n"
	assert result.stderr == ""
	assert gh_count == "2"
	assert sleep_log == "30\n"


def test_comprehensive_release_helper_stops_after_max_attempts_on_rate_limit() -> None:
	gh_script = textwrap.dedent(
		"""\
		#!/usr/bin/env bash
		count="$(cat \"${TEST_GH_COUNT_FILE}\" 2>/dev/null || echo 0)"
		count=$((count + 1))
		printf '%s' "${count}" > "${TEST_GH_COUNT_FILE}"
		echo "secondary rate limit" >&2
		exit 1
		"""
	)
	result, gh_count, sleep_log = _run_comprehensive_release_helper(
		gh_script=gh_script,
		shell_body=textwrap.dedent(
			"""\
			gh_api_safe "repos/example/repo"
			status=$?
			printf 'status=%s\n' "${status}"
			printf 'output=%s\n' "${GH_API_SAFE_OUTPUT}"
			printf 'backoff=%s\n' "${RATE_LIMIT_BACKOFF}"
			exit "${status}"
			"""
		),
	)

	assert result.returncode == 1
	assert result.stdout == "status=1\noutput=\nbackoff=120\n"
	assert result.stderr == ""
	assert gh_count == "4"
	assert sleep_log == "30\n60\n120\n"


def test_comprehensive_release_helper_quiet_mode_retries_rate_limit_once_without_reporting() -> None:
	gh_script = textwrap.dedent(
		"""\
		#!/usr/bin/env bash
		count="$(cat \"${TEST_GH_COUNT_FILE}\" 2>/dev/null || echo 0)"
		count=$((count + 1))
		printf '%s' "${count}" > "${TEST_GH_COUNT_FILE}"
		if [ "${count}" -eq 1 ]; then
			echo "secondary rate limit" >&2
			exit 1
		fi
		printf 'success payload'
		"""
	)
	result, gh_count, sleep_log = _run_comprehensive_release_helper(
		gh_script=gh_script,
		shell_body=textwrap.dedent(
			"""\
			gh_api_safe_quiet "repos/example/repo"
			status=$?
			printf 'status=%s\n' "${status}"
			printf 'output=%s\n' "${GH_API_SAFE_OUTPUT}"
			printf 'backoff=%s\n' "${RATE_LIMIT_BACKOFF}"
			exit "${status}"
			"""
		),
	)

	assert result.returncode == 0
	assert result.stdout == "status=0\noutput=success payload\nbackoff=0\n"
	assert result.stderr == ""
	assert gh_count == "2"
	assert sleep_log == "30\n"


def test_comprehensive_release_helper_non_rate_limit_failure_keeps_existing_reporting() -> None:
	gh_script = textwrap.dedent(
		"""\
		#!/usr/bin/env bash
		count="$(cat \"${TEST_GH_COUNT_FILE}\" 2>/dev/null || echo 0)"
		count=$((count + 1))
		printf '%s' "${count}" > "${TEST_GH_COUNT_FILE}"
		echo "boom failure" >&2
		exit 1
		"""
	)
	result, gh_count, sleep_log = _run_comprehensive_release_helper(
		gh_script=gh_script,
		shell_body='gh_api_safe "repos/example/repo"',
	)

	assert result.returncode == 1
	assert result.stdout == "::error::gh api call failed: repos/example/repo\n"
	assert result.stderr == "boom failure\n"
	assert gh_count == "1"
	assert sleep_log == ""


def test_comprehensive_release_helper_quiet_mode_suppresses_non_rate_limit_reporting() -> None:
	gh_script = textwrap.dedent(
		"""\
		#!/usr/bin/env bash
		count="$(cat \"${TEST_GH_COUNT_FILE}\" 2>/dev/null || echo 0)"
		count=$((count + 1))
		printf '%s' "${count}" > "${TEST_GH_COUNT_FILE}"
		echo "boom failure" >&2
		exit 1
		"""
	)
	result, gh_count, sleep_log = _run_comprehensive_release_helper(
		gh_script=gh_script,
		shell_body='gh_api_safe_quiet "repos/example/repo"',
	)

	assert result.returncode == 1
	assert result.stdout == ""
	assert result.stderr == ""
	assert gh_count == "1"
	assert sleep_log == ""


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
