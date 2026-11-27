#!/usr/bin/env python3
"""Clarify stops retrying when the isolated runner rejects the source snapshot (#9837).

scripts/clarify_isolated_run.sh prints the fixed line
`clarify source snapshot rejected` and exits 1 when it refuses to copy the
checkout. The checkout does not change between attempts, so the `Run Codex`
step of clarify.yml must stop after the first such attempt instead of
retrying the same rejection three times.
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CLARIFY_WF = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
HELPER = REPO_ROOT / "scripts" / "clarify_isolated_run.sh"
REJECTION_LINE = "clarify source snapshot rejected"
STOP_ERROR = (
	"clarify stopped after attempt 1: scripts/clarify_isolated_run.sh rejected the source snapshot, "
	"and a retry gets the same result."
)

STUB_RUNNER = r"""#!/usr/bin/env bash
# Stub of scripts/clarify_isolated_run.sh: $4 is "claude" on the Claude path.
engine="${4:-codex}"
echo "${engine}" >> "${RUNTIME_DIR}/calls"
case "${STUB_MODE}" in
  reject)
    echo "clarify source snapshot rejected" >&2
    exit 1
    ;;
  fallback_reject)
    if [ "${engine}" = "claude" ]; then
      exit 75
    fi
    echo "clarify source snapshot rejected" >&2
    exit 1
    ;;
  prefixed)
    echo "x clarify source snapshot rejected" >&2
    exit 1
    ;;
  suffixed)
    echo "clarify source snapshot rejected: extra" >&2
    exit 1
    ;;
  reject_but_ok)
    echo "clarify source snapshot rejected" >&2
    printf 'questions\n' > "$2"
    exit 0
    ;;
esac
echo "other failure" >&2
exit 1
"""


def _workflow_lines() -> list[str]:
	return CLARIFY_WF.read_text(encoding="utf-8").splitlines()


def _attempt_script() -> str:
	"""The attempt loop with its helper functions, dedented to plain bash."""
	lines = _workflow_lines()
	start = next(i for i, line in enumerate(lines) if line.strip().startswith("# Issue #8602: each failed attempt"))
	stop = next(i for i in range(start, len(lines)) if lines[i].strip() == "- name: Semantic cache store")
	end = max(i for i in range(start, stop) if lines[i].strip() == "done")
	return textwrap.dedent("\n".join(lines[start : end + 1])) + "\n"


def _loop_only() -> list[str]:
	script = _attempt_script().splitlines()
	start = next(i for i, line in enumerate(script) if line.strip() == "max_attempts=3")
	return script[start:]


def _run(mode: str, engine: str = "claude") -> tuple[subprocess.CompletedProcess[str], list[str], int]:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		(tmp / "scripts").mkdir()
		stub = tmp / "scripts" / "clarify_isolated_run.sh"
		stub.write_text(STUB_RUNNER, encoding="utf-8")
		stub.chmod(0o755)
		(tmp / "p.txt").write_text("prompt\n", encoding="utf-8")
		env = {
			"PATH": "/usr/bin:/bin",
			"STUB_MODE": mode,
			"CLARIFY_ENGINE": engine,
			"RUNTIME_DIR": str(tmp),
			"CODEX_PROMPT_FILE": str(tmp / "p.txt"),
			"CODEX_OUTPUT_FILE": str(tmp / "o.txt"),
			"GITHUB_ENV": str(tmp / "github_env"),
		}
		script = 'set -euo pipefail\nsleep() { echo slept >> "${RUNTIME_DIR}/sleeps"; }\n' + _attempt_script()
		result = subprocess.run(
			["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True, timeout=120, check=False
		)
		calls_file = tmp / "calls"
		sleeps_file = tmp / "sleeps"
		calls = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []
		sleeps = len(sleeps_file.read_text(encoding="utf-8").splitlines()) if sleeps_file.exists() else 0
	return result, calls, sleeps


def _attempt_failed_lines(stdout: str) -> list[str]:
	return [line for line in stdout.splitlines() if line.startswith("CLARIFY_ENGINE_ATTEMPT_FAILED ")]


def test_helper_and_workflow_use_the_same_rejection_line() -> None:
	helper = HELPER.read_text(encoding="utf-8")
	assert f'raise SystemExit("{REJECTION_LINE}")' in helper
	loop = "\n".join(_loop_only())
	assert f"grep -a -qx '{REJECTION_LINE}' \"${{clarify_attempt_stderr_file}}\"" in loop


def test_every_runner_call_is_inside_the_stderr_capture_pair() -> None:
	loop = _loop_only()
	# The loop calls the helper as `bash "${SUPPORT_SCRIPTS_DIR:-scripts}/clarify_isolated_run.sh"
	# "${CODEX_PROMPT_FILE}" ...`; match on the helper name plus its prompt argument.
	call_indexes = [i for i, line in enumerate(loop) if 'clarify_isolated_run.sh" "${CODEX_PROMPT_FILE}"' in line]
	assert len(call_indexes) == 2, loop
	for i in call_indexes:
		assert loop[i - 1].strip() == "_clarify_attempt_stderr_capture_start || true", loop[i - 1]
		assert loop[i + 1].strip() == "_clarify_attempt_stderr_capture_stop || true", loop[i + 1]


def test_claude_rejection_stops_after_one_attempt() -> None:
	result, calls, sleeps = _run("reject", "claude")
	assert result.returncode == 1, result.stdout + result.stderr
	assert calls == ["claude"]
	assert sleeps == 0
	assert "Retrying in" not in result.stdout
	failed = _attempt_failed_lines(result.stdout)
	assert len(failed) == 1, result.stdout
	assert "engine=Claude attempt=1 max_attempts=3 kind=nonzero_exit exit_code=1 " in failed[0]
	assert f"::error::Claude {STOP_ERROR}" in result.stdout
	assert "failed after 3 attempts" not in result.stdout
	# The runner's line still reaches the job log.
	assert REJECTION_LINE in result.stderr


def test_codex_rejection_stops_after_one_attempt() -> None:
	result, calls, sleeps = _run("reject", "codex")
	assert result.returncode == 1, result.stdout + result.stderr
	assert calls == ["codex"]
	assert sleeps == 0
	failed = _attempt_failed_lines(result.stdout)
	assert len(failed) == 1, result.stdout
	assert "engine=Codex attempt=1 max_attempts=3 kind=nonzero_exit exit_code=1 " in failed[0]
	assert f"::error::Codex {STOP_ERROR}" in result.stdout
	assert REJECTION_LINE in result.stderr


def test_rejection_after_claude_fallback_stops_after_one_attempt() -> None:
	result, calls, sleeps = _run("fallback_reject", "claude")
	assert result.returncode == 1, result.stdout + result.stderr
	assert calls == ["claude", "codex"]
	assert sleeps == 0
	failed = _attempt_failed_lines(result.stdout)
	assert len(failed) == 1, result.stdout
	assert "engine=Codex attempt=1 " in failed[0]
	assert f"::error::Codex {STOP_ERROR}" in result.stdout


def test_partial_matches_keep_all_three_attempts() -> None:
	for mode in ("prefixed", "suffixed", "other"):
		result, calls, sleeps = _run(mode, "claude")
		assert result.returncode == 1, (mode, result.stdout, result.stderr)
		assert calls == ["claude", "claude", "claude"], mode
		assert sleeps == 2, mode
		assert len(_attempt_failed_lines(result.stdout)) == 3, mode
		assert "rejected the source snapshot" not in result.stdout, mode
		assert "::error::Claude clarify failed after 3 attempts." in result.stdout, mode


def test_rejection_line_with_successful_output_is_not_a_failure() -> None:
	result, calls, sleeps = _run("reject_but_ok", "claude")
	assert result.returncode == 0, result.stdout + result.stderr
	assert calls == ["claude"]
	assert sleeps == 0
	assert "Claude clarify succeeded on attempt 1." in result.stdout
	assert "rejected the source snapshot" not in result.stdout


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
