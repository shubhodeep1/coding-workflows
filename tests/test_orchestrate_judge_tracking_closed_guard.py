#!/usr/bin/env python3
"""The poller's judge creates no issues for a closed tracking issue (#8953).

Six minutes after the release gate closed smoke tracking issue 8834, the
judge's `judge-addition` path created "Re-deliver" issues #8837/#8838, whose
PRs stayed open. `judge_tracking_issue_closed_skip` re-reads the tracking
issue state (fail-open) before either judge path creates issues.

The cases are collected by tests/test_test_and_mark_stable_orchestrate_child_count.py,
which CI already runs.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _script_text() -> str:
	return SCRIPT.read_text(encoding="utf-8")


def _guard_function() -> str:
	text = _script_text()
	match = re.search(r"^judge_tracking_issue_closed_skip\(\) \{\n.*?^\}\n", text, re.S | re.M)
	assert match, "judge_tracking_issue_closed_skip() is missing"
	return match.group(0)


def _run_guard(stub_body: str, tracking: str = "8834", guard_type: str = "judge-addition") -> subprocess.CompletedProcess:
	script = (
		"set -euo pipefail\n"
		f"get_issue_state_labels_json() {{ {stub_body}; }}\n"
		+ _guard_function()
		+ f'if judge_tracking_issue_closed_skip {guard_type}; then echo RC=0; else echo RC=1; fi\n'
	)
	env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TRACKING_NUM": tracking}
	return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)


def test_closed_tracking_issue_skips_and_logs() -> None:
	result = _run_guard("""echo '{"state":"closed","state_reason":"not_planned","labels":[]}'""")
	assert result.returncode == 0, result.stderr
	assert "ORCHESTRATE_JUDGE_ADDITION_SKIP reason=tracking_closed tracking=8834 type=judge-addition" in result.stdout
	assert "RC=0" in result.stdout


def test_open_tracking_issue_keeps_behaviour() -> None:
	result = _run_guard("""echo '{"state":"open","state_reason":"","labels":[]}'""")
	assert result.returncode == 0, result.stderr
	assert "RC=1" in result.stdout
	assert "ORCHESTRATE_JUDGE_ADDITION_SKIP" not in result.stdout


def test_read_error_fails_open() -> None:
	for stub in ("return 1", "echo 'not json'", "echo ''"):
		result = _run_guard(stub)
		assert result.returncode == 0, result.stderr
		assert "RC=1" in result.stdout, (stub, result.stdout)


def test_invalid_tracking_number_does_not_read() -> None:
	result = _run_guard("echo CALLED >&2; echo '{\"state\":\"closed\"}'", tracking="")
	assert "RC=1" in result.stdout
	assert "CALLED" not in result.stderr


def test_judge_addition_arm_is_guarded_before_issue_create() -> None:
	text = _script_text()
	guard = text.index('if [ "${NEW_ISSUES_COUNT}" -gt 0 ] && ! judge_tracking_issue_closed_skip judge-addition; then')
	create = text.index("- Type: judge-addition", guard)
	# The guarded block is the one holding the judge-addition issue body.
	assert guard < text.index("gh issue create", guard) and create - guard < 20000
	# No close of the guarded `if` (six-space `fi`) before the issue create.
	assert "\n      fi\n" not in text[guard:create]
	assert text.rfind("in_progress)", 0, guard) > text.rfind("create_judge_fixup_issues_from_verdict() {", 0, guard)


def test_fixup_function_is_guarded_at_its_top() -> None:
	text = _script_text()
	start = text.index("create_judge_fixup_issues_from_verdict() {")
	first_if = text.index("\n", start) + 1
	line = text[first_if:text.index("\n", first_if)]
	assert line.strip() == 'if [ "${NEW_ISSUES_COUNT}" -gt 0 ] && ! judge_tracking_issue_closed_skip judge-fix-up; then'
	assert text.index("judge_tracking_issue_closed_skip() {") < start


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
