#!/usr/bin/env python3
"""Tests for the claude/** push route's PR grace window.

Background (glm-5.2 usage analysis, 2026-09-23..30): Claude sessions push a
new ``claude/**`` branch and open its PR a few seconds later (median 20s over
151 cases).  ``resolve-claude-branch-pr`` in ``.github/workflows/
internal-review.yml`` looked up the open PR once, at push time, found none,
and dispatched the no-PR reviewer panel; ``pull_request:opened`` then reviewed
the same commit again.  About 9% of the week's glm-5.2 reviewer tokens went
to those duplicates.

The step now re-checks every 60s for up to
``CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS`` (default 300) before proceeding.
These tests run the step's real ``run:`` body under bash with ``gh`` and
``sleep`` stubbed on ``PATH``, so no test actually waits.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "internal-review.yml"

GH_STUB = r"""#!/usr/bin/env bash
# Records every call; answers the open-PR lookup from a comma-separated
# script of responses (a PR number, "none", or "fail"), one per call.
stub_dir="$(dirname "$0")"
echo "$*" >> "${stub_dir}/gh_calls.log"
case "$*" in
  *"/pulls?"*)
    count_file="${stub_dir}/pull_lookups"
    n=$(( $(cat "${count_file}" 2>/dev/null || echo 0) + 1 ))
    echo "${n}" > "${count_file}"
    IFS=',' read -r -a responses <<< "${GH_STUB_PULL_RESPONSES}"
    idx=$(( n - 1 ))
    if [ "${idx}" -ge "${#responses[@]}" ]; then
      idx=$(( ${#responses[@]} - 1 ))
    fi
    answer="${responses[${idx}]}"
    case "${answer}" in
      fail) echo "HTTP 502" >&2; exit 1 ;;
      silentfail) exit 1 ;;
      ratelimit)
        printf 'gh: API rate limit exceeded for user ID 11442166. "quoted"\tpart %s\nsecond line\n' "$(printf 'x%.0s' $(seq 1 300))" >&2
        exit 1
        ;;
      none) exit 0 ;;
      *) echo "${answer}"; exit 0 ;;
    esac
    ;;
  *)
    echo "stub-default-branch"
    ;;
esac
"""

SLEEP_STUB = r"""#!/usr/bin/env bash
echo "$1" >> "$(dirname "$0")/sleep_calls.log"
"""


def _resolve_step() -> dict:
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	steps = workflow["jobs"]["resolve-claude-branch-pr"]["steps"]
	matches = [step for step in steps if step.get("id") == "resolve"]
	assert len(matches) == 1, "expected exactly one step with id=resolve"
	return matches[0]


def _run(
	pull_responses: str,
	grace: str | None = "300",
	default_branch: str = "main",
	head_ref: str = "claude/implement-plan-issue-42-phase-1",
	tmpdir: str | None = None,
) -> dict:
	script = _resolve_step()["run"]
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		stub_dir = tmp_path / "bin"
		stub_dir.mkdir()
		for name, body in (("gh", GH_STUB), ("sleep", SLEEP_STUB)):
			stub = stub_dir / name
			stub.write_text(body, encoding="utf-8")
			stub.chmod(0o755)
		output_file = tmp_path / "github_output"
		output_file.write_text("", encoding="utf-8")
		env = {
			"PATH": "%s:%s" % (stub_dir, os.environ.get("PATH", "")),
			"HOME": str(tmp_path),
			"GITHUB_OUTPUT": str(output_file),
			"GH_TOKEN": "stub-token",
			"REPOSITORY": "octo/coding-workflows",
			"HEAD_REF": head_ref,
			"HEAD_SHA": "0123456789abcdef0123456789abcdef01234567",
			"EVENT_DEFAULT_BRANCH": default_branch,
			"GH_STUB_PULL_RESPONSES": pull_responses,
		}
		if grace is not None:
			env["CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS"] = grace
		if tmpdir is not None:
			env["TMPDIR"] = tmpdir
		proc = subprocess.run(
			["bash", "-c", script],
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		outputs = {}
		for line in output_file.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value

		def _lines(name: str) -> list[str]:
			path = stub_dir / name
			if not path.exists():
				return []
			return path.read_text(encoding="utf-8").splitlines()

		return {
			"rc": proc.returncode,
			"stdout": proc.stdout,
			"stderr": proc.stderr,
			"outputs": outputs,
			"sleeps": [int(value) for value in _lines("sleep_calls.log")],
			"gh_calls": _lines("gh_calls.log"),
		}


def test_existing_pr_skips_without_waiting():
	result = _run("123")
	assert result["rc"] == 0, result["stderr"]
	assert result["outputs"]["proceed"] == "false"
	assert result["sleeps"] == []
	assert "RESOLVE_CLAUDE_BRANCH_PR_SKIP" in result["stdout"]
	assert "existing_pr=123" in result["stdout"]
	assert "waited_secs=0" in result["stdout"]


def test_pr_opened_during_window_skips_review():
	result = _run("none,none,77")
	assert result["rc"] == 0, result["stderr"]
	assert result["outputs"]["proceed"] == "false"
	assert result["sleeps"] == [60, 60]
	assert "existing_pr=77" in result["stdout"]
	assert "waited_secs=120" in result["stdout"]
	assert result["stdout"].count("RESOLVE_CLAUDE_BRANCH_PR_WAIT") == 2


def test_no_pr_after_default_window_proceeds():
	result = _run("none", grace=None)
	assert result["rc"] == 0, result["stderr"]
	assert result["outputs"]["proceed"] == "true"
	assert result["outputs"]["base_ref"] == "main"
	assert result["outputs"]["head_ref"] == "claude/implement-plan-issue-42-phase-1"
	assert result["outputs"]["head_sha"] == "0123456789abcdef0123456789abcdef01234567"
	assert result["sleeps"] == [60, 60, 60, 60, 60]
	lookups = [call for call in result["gh_calls"] if "/pulls?" in call]
	assert len(lookups) == 6
	assert "RESOLVE_CLAUDE_BRANCH_PR_PROCEED" in result["stdout"]
	assert "waited_secs=300 grace_secs=300 lookup_failures=0" in result["stdout"]


def test_lookup_targets_open_prs_for_encoded_head_branch():
	result = _run("5")
	lookups = [call for call in result["gh_calls"] if "/pulls?" in call]
	assert len(lookups) == 1
	assert (
		"repos/octo/coding-workflows/pulls?state=open"
		"&head=octo:claude%2Fimplement-plan-issue-42-phase-1"
	) in lookups[0]


def test_partial_last_interval():
	result = _run("none", grace="90")
	assert result["outputs"]["proceed"] == "true"
	assert result["sleeps"] == [60, 30]
	assert "waited_secs=90 grace_secs=90" in result["stdout"]


def test_zero_grace_restores_single_lookup():
	result = _run("none", grace="0")
	assert result["rc"] == 0, result["stderr"]
	assert result["outputs"]["proceed"] == "true"
	assert result["sleeps"] == []
	lookups = [call for call in result["gh_calls"] if "/pulls?" in call]
	assert len(lookups) == 1
	assert "RESOLVE_CLAUDE_BRANCH_PR_WAIT" not in result["stdout"]


def test_invalid_grace_falls_back_to_default_with_warning():
	for bad in ("abc", "-5", "3601", "999999", "12.5", ""):
		result = _run("none", grace=bad)
		assert result["rc"] == 0, (bad, result["stderr"])
		assert result["outputs"]["proceed"] == "true", bad
		assert result["sleeps"] == [60, 60, 60, 60, 60], bad
		if bad:
			assert "::warning::CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS=" in result["stdout"], bad


def test_max_grace_boundary_is_accepted():
	result = _run("none", grace="3600")
	assert result["rc"] == 0, result["stderr"]
	assert "::warning::" not in result["stdout"]
	assert result["sleeps"] == [60] * 60
	assert "waited_secs=3600 grace_secs=3600" in result["stdout"]


def test_lookup_error_file_is_removed_on_exit():
	with tempfile.TemporaryDirectory() as tmpdir:
		result = _run("fail,none", grace="60", tmpdir=tmpdir)
		assert result["rc"] == 0, result["stderr"]
		assert "RESOLVE_CLAUDE_BRANCH_PR_LOOKUP_FAILED" in result["stdout"]
		assert os.listdir(tmpdir) == []


def test_leading_zero_grace_is_decimal():
	result = _run("none", grace="090")
	assert result["sleeps"] == [60, 30]


def test_lookup_failures_keep_polling_then_skip():
	result = _run("fail,fail,42")
	assert result["rc"] == 0, result["stderr"]
	assert result["outputs"]["proceed"] == "false"
	assert result["sleeps"] == [60, 60]
	assert result["stdout"].count("RESOLVE_CLAUDE_BRANCH_PR_LOOKUP_FAILED") == 2
	assert result["stdout"].count('error="HTTP 502"') == 2
	assert "existing_pr=42" in result["stdout"]


def test_lookup_failures_fail_open_after_window():
	result = _run("fail")
	assert result["rc"] == 0, result["stderr"]
	assert result["outputs"]["proceed"] == "true"
	assert result["sleeps"] == [60, 60, 60, 60, 60]
	assert "lookup_failures=6" in result["stdout"]


def test_lookup_failure_logs_sanitized_one_line_error():
	result = _run("ratelimit,7")
	assert result["rc"] == 0, result["stderr"]
	assert result["outputs"]["proceed"] == "false"
	failed = [line for line in result["stdout"].splitlines() if line.startswith("RESOLVE_CLAUDE_BRANCH_PR_LOOKUP_FAILED")]
	assert len(failed) == 1
	line = failed[0]
	assert 'error="gh: API rate limit exceeded for user ID 11442166. quoted part xxx' in line
	assert "second line" not in line
	error_text = line.split('error="', 1)[1]
	assert error_text.endswith('"')
	error_text = error_text[:-1]
	assert len(error_text) <= 200
	assert '"' not in error_text
	assert "\t" not in error_text


def test_lookup_failure_without_error_text_says_so():
	result = _run("silentfail,7")
	assert result["outputs"]["proceed"] == "false"
	assert 'error="no error text"' in result["stdout"]


def test_missing_default_branch_falls_back_to_repo_lookup():
	result = _run("none", grace="0", default_branch="")
	assert result["outputs"]["proceed"] == "true"
	assert result["outputs"]["base_ref"] == "stub-default-branch"


def test_workflow_wires_grace_variable_with_default():
	env = _resolve_step()["env"]
	assert env["CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS"] == (
		"${{ vars.CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS || '300' }}"
	)


if __name__ == "__main__":
	tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
	for name, fn in tests:
		fn()
		print("PASS %s" % name)
	print("%d tests passed" % len(tests))
