#!/usr/bin/env python3
"""Contract test: every script orchestrate_poll_process.sh invokes by
path must be staged into the consumer workspace by orchestrate_poll.yml.

Regression pin for the tele-funtoken-msg-scoring tracking-body-sync
warning (poller run 32657328962): orchestrate_poll_process.sh calls
``python3 scripts/check_integration_pr_readiness.py`` but the script was
missing from the workflow's support-file stage list, so every consumer
poll run failed the call with "[Errno 2] No such file or directory" and
the orchestrator/integration-pr-not-ready commit status was never
refreshed from the poller.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _stage_list() -> list[str]:
	text = WORKFLOW.read_text(encoding="utf-8")
	m = re.search(r"for f in (gh_helpers\.sh [^;]+); do", text)
	assert m, "Failed to locate the support-script stage list in orchestrate_poll.yml"
	return m.group(1).split()


def test_stage_list_includes_integration_pr_readiness_script() -> None:
	staged = _stage_list()
	assert "check_integration_pr_readiness.py" in staged, staged


def test_poll_process_python_script_invocations_are_staged() -> None:
	# Every unguarded `python3 scripts/<f>.py` call site in the poller
	# must have a matching entry in the stage list, or consumer
	# workspaces (which only contain staged support files) fail the call
	# at runtime. Invocations wrapped in their own `[ -f scripts/<f> ]`
	# existence check (e.g. verify_integration_fingerprints.py) are
	# deliberately optional and exempt.
	staged = set(_stage_list())
	source = POLL_PROCESS.read_text(encoding="utf-8")
	invoked = set(re.findall(r"python3\s+(?:\"?\$\{?SCRIPT_DIR\}?\"?/|scripts/)([A-Za-z0-9_]+\.py)", source))
	existence_guarded = set(re.findall(r"\[ -f \"?scripts/([A-Za-z0-9_]+\.py)\"? \]", source))
	missing = sorted(f for f in invoked if f not in staged and f not in existence_guarded)
	assert not missing, f"invoked by orchestrate_poll_process.sh but not staged: {missing}"


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
