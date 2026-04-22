#!/usr/bin/env python3
"""Contract tests for .github/workflows/validation-refresh.yml."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "validation-refresh.yml"


def test_validation_refresh_workflow_has_required_triggers() -> None:
	content = WORKFLOW_PATH.read_text(encoding="utf-8")
	assert '"on":' in content
	assert "schedule:" in content
	assert "workflow_dispatch:" in content
	assert "cron:" in content


def test_validation_refresh_workflow_invokes_runner_with_auth() -> None:
	content = WORKFLOW_PATH.read_text(encoding="utf-8")
	assert "scripts/validation_refresh_runner.py" in content
	assert "GH_TOKEN: ${{ secrets.GH_PAT || github.token }}" in content
	assert "pull-requests: write" in content
	assert "contents: write" in content
	assert "--summary-json" in content


def test_validation_refresh_workflow_summarizes_and_notifies_on_failure() -> None:
	content = WORKFLOW_PATH.read_text(encoding="utf-8")
	assert "GITHUB_STEP_SUMMARY" in content
	assert "Telegram failure notification" in content
	assert "if: failure()" in content


# ---------------------------------------------------------------------------
# Runner
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
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
