#!/usr/bin/env python3
"""Behaviour tests for the "Run Codex planning" failure diagnostic (#9266).

A failed plan step now prints one ``::error::RUN_PLAN_CODEX_FAILED`` line
naming the runner's last exception, so the heal intake keeps it. The step's
exit code, stdout and stderr stay unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plan.yml"
STEP_NAME = "Run Codex planning"
DIAGNOSTIC_PREFIX = "::error::RUN_PLAN_CODEX_FAILED"


def _workflow_step(workflow_text: str, step_name: str) -> str:
	marker = f"      - name: {step_name}\n"
	start = workflow_text.find(marker)
	assert start != -1, f"missing workflow step: {step_name}"
	end = workflow_text.find("\n      - name:", start + len(marker))
	return workflow_text[start:] if end == -1 else workflow_text[start:end]


def _step_run_body(step: str) -> str:
	body = step.split("        run: |\n", 1)[1]
	return "".join(
		line[10:] if line.startswith("          ") else line.lstrip(" ")
		for line in body.splitlines(keepends=True)
	)


def _body() -> str:
	return _step_run_body(_workflow_step(PLAN_WORKFLOW.read_text(encoding="utf-8"), STEP_NAME))


def _clean_env(**extra: str) -> dict[str, str]:
	environment = {
		key: value
		for key, value in os.environ.items()
		if not key.startswith("GIT_") and key not in ("BASH_ENV", "ENV")
	}
	environment.update(extra)
	return environment


def _run(
	runner_script: str,
	heal_route_script: str = "print('false')\n",
	pipefail: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
	tmp = tempfile.mkdtemp(prefix="plan-runner-diag-")
	root = Path(tmp)
	scripts = root / "scripts"
	scripts.mkdir()
	(scripts / "workflow_failure_heal.py").write_text(
		"import sys\n" + heal_route_script, encoding="utf-8"
	)
	(scripts / "run_plan_codex.sh").write_text(runner_script, encoding="utf-8")
	runner_temp = root / "runner-temp"
	runner_temp.mkdir()
	(root / "issue.json").write_text("{}", encoding="utf-8")
	(root / "github_env").write_text("", encoding="utf-8")
	body_file = root / "body.sh"
	body_file.write_text(_body(), encoding="utf-8")
	flags = ["-eo", "pipefail"] if pipefail else ["-e"]
	result = subprocess.run(
		["bash", "--noprofile", "--norc", *flags, str(body_file)],
		cwd=root,
		env=_clean_env(
			ISSUE_META_FILE=str(root / "issue.json"),
			GITHUB_ENV=str(root / "github_env"),
			RUNTIME_DIR=str(root),
			RUNNER_TEMP=str(runner_temp),
		),
		capture_output=True, text=True, check=False, timeout=60,
	)
	return result, runner_temp


CHAINED_TRACEBACK_RUNNER = r"""
echo out-line
{
  echo 'Traceback (most recent call last):'
  echo '  File "x.py", line 1, in <module>'
  echo "KeyError: 'x'"
  echo ''
  echo 'During handling of the above exception, another exception occurred:'
  echo ''
  echo 'Traceback (most recent call last):'
  echo '  File "x.py", line 3, in <module>'
  printf 'ValueError: 50%% bad\r\n'
} >&2
exit 3
"""


def _diagnostic_lines(stderr: str) -> list[str]:
	return [line for line in stderr.splitlines() if "RUN_PLAN_CODEX_FAILED" in line]


def test_step_keeps_heal_route_and_runner_call() -> None:
	body = _body()
	assert 'python3 scripts/workflow_failure_heal.py heal-route --issue-json "${ISSUE_META_FILE}"' in body
	assert "echo '::error::Heal route classification failed' >&2" in body
	assert "bash scripts/run_plan_codex.sh" in body
	assert "${{" not in body


def test_chained_traceback_names_last_exception_and_keeps_rc() -> None:
	for pipefail in (False, True):
		result, runner_temp = _run(CHAINED_TRACEBACK_RUNNER, pipefail=pipefail)
		assert result.returncode == 3, (pipefail, result.stderr)
		assert "out-line" in result.stdout
		assert "out-line" not in result.stderr
		assert "KeyError: 'x'" in result.stderr
		assert "During handling of the above exception" in result.stderr
		assert _diagnostic_lines(result.stderr) == [
			f"{DIAGNOSTIC_PREFIX} rc=3 exception=ValueError: 50%25 bad"
		], (pipefail, result.stderr)
		assert list(runner_temp.iterdir()) == []


def test_success_prints_no_diagnostic() -> None:
	for pipefail in (False, True):
		result, runner_temp = _run("echo fine\necho note >&2\nexit 0\n", pipefail=pipefail)
		assert result.returncode == 0, result.stderr
		assert "fine" in result.stdout
		assert "note" in result.stderr
		assert _diagnostic_lines(result.stderr) == []
		assert list(runner_temp.iterdir()) == []


def test_failure_without_exception_line_reports_none() -> None:
	for pipefail in (False, True):
		result, _runner_temp = _run("echo boom >&2\nexit 1\n", pipefail=pipefail)
		assert result.returncode == 1
		assert _diagnostic_lines(result.stderr) == [f"{DIAGNOSTIC_PREFIX} rc=1 exception=none"]


def test_long_exception_is_cut_to_one_short_line() -> None:
	long_message = "RuntimeError: " + "y" * 1000
	result, _runner_temp = _run(f"echo '{long_message}' >&2\nexit 2\n")
	assert result.returncode == 2
	lines = _diagnostic_lines(result.stderr)
	assert len(lines) == 1
	exception = lines[0].split("exception=", 1)[1]
	assert exception == long_message[:300]


def test_percent_heavy_exception_stays_within_300_characters() -> None:
	# The cut runs after `%` encoding, so the emitted field never exceeds 300.
	percent_message = "RuntimeError: " + "%" * 400
	result, _runner_temp = _run(f"printf '%s\\n' '{percent_message}' >&2\nexit 2\n")
	assert result.returncode == 2
	lines = _diagnostic_lines(result.stderr)
	assert len(lines) == 1
	exception = lines[0].split("exception=", 1)[1]
	assert len(exception) == 300
	assert exception == percent_message.replace("%", "%25")[:300]


def test_nul_byte_in_stderr_keeps_the_exception_line() -> None:
	# `grep -a`: a NUL byte must not turn the copy into a "binary file"
	# whose matching lines grep would suppress.
	result, _runner_temp = _run("printf 'noise\\000x\\n' >&2\necho \"KeyError: 'k'\" >&2\nexit 1\n")
	assert result.returncode == 1
	assert _diagnostic_lines(result.stderr) == [f"{DIAGNOSTIC_PREFIX} rc=1 exception=KeyError: 'k'"]


def test_mktemp_failure_runs_the_runner_unchanged() -> None:
	# The fallback branch: no stderr copy, so the runner is exec'd with its
	# exit code and output kept and no diagnostic line printed.
	for runner_script, expected_rc in (("echo fine\nexit 0\n", 0), ("echo fine\necho \"KeyError: 'x'\" >&2\nexit 4\n", 4)):
		tmp = Path(tempfile.mkdtemp(prefix="plan-runner-diag-"))
		(tmp / "scripts").mkdir()
		(tmp / "scripts" / "workflow_failure_heal.py").write_text("print('false')\n", encoding="utf-8")
		(tmp / "scripts" / "run_plan_codex.sh").write_text(runner_script, encoding="utf-8")
		(tmp / "issue.json").write_text("{}", encoding="utf-8")
		(tmp / "github_env").write_text("", encoding="utf-8")
		(tmp / "body.sh").write_text(_body(), encoding="utf-8")
		result = subprocess.run(
			["bash", "--noprofile", "--norc", "-e", str(tmp / "body.sh")],
			cwd=tmp,
			env=_clean_env(
				ISSUE_META_FILE=str(tmp / "issue.json"),
				GITHUB_ENV=str(tmp / "github_env"),
				RUNTIME_DIR=str(tmp),
				RUNNER_TEMP=str(tmp / "missing-runner-temp"),
			),
			capture_output=True, text=True, check=False, timeout=60,
		)
		assert result.returncode == expected_rc, result.stderr
		assert "fine" in result.stdout
		assert _diagnostic_lines(result.stderr) == []
		assert not (tmp / "missing-runner-temp").exists()


def test_heal_route_failure_is_unchanged() -> None:
	result, runner_temp = _run(
		"echo RUNNER_RAN\nexit 0\n", heal_route_script="sys.exit(1)\n"
	)
	assert result.returncode == 1
	assert "::error::Heal route classification failed" in result.stderr
	assert "RUNNER_RAN" not in result.stdout
	assert _diagnostic_lines(result.stderr) == []
	assert list(runner_temp.iterdir()) == []


def test_heal_intake_keeps_the_diagnostic_line() -> None:
	sys.path.insert(0, str(REPO_ROOT / "scripts"))
	import workflow_failure_heal  # noqa: E402

	line = f"{DIAGNOSTIC_PREFIX} rc=1 exception=KeyError: 'x'"
	assert workflow_failure_heal._SOFT_LOG_PATTERNS.search(line)
	log = "\n".join(
		[f"2026-11-11T14:47:28.0000000Z {line}"]
		+ [f"2026-11-11T14:47:29.0000000Z filler line {index}" for index in range(500)]
	) + "\n"
	assert "RUN_PLAN_CODEX_FAILED rc=1 exception=KeyError" in workflow_failure_heal.filter_log(log)


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
