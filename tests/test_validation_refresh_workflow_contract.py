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
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in content
	assert (
		"VALIDATION_DISCOVERY_ENABLED: ${{ github.event.inputs.discovery_enabled || "
		"vars.VALIDATION_DISCOVERY_ENABLED || 'true' }}"
	) in content
	# The runner is read-only against consumer repos: no commits, pushes, or
	# pull requests are produced, so the workflow must NOT request write perms
	# that imply otherwise.
	assert "pull-requests: write" not in content
	assert "contents: write" not in content
	assert "contents: read" in content
	assert "--summary-json" in content


def test_validation_refresh_workflow_wires_codex_provider_auth() -> None:
	# Discovery dispatch invokes `codex exec` (via the refresh runner), which
	# can only authenticate to the model provider when (a) ~/.codex/config.toml
	# defines model_provider=openrouter + env_key=OPENROUTER_API_KEY (written by
	# scripts/write_codex_config.sh) and (b) the OPENROUTER_API_KEY secret is
	# present in the step env. PR #2959 shipped discovery without either, so
	# every `codex exec` exited codex_rc_nonzero(rc=1) and the daily refresh
	# silently degraded to drift-monitoring only. Lock both halves in so the
	# provider wiring can't regress again.
	content = WORKFLOW_PATH.read_text(encoding="utf-8")
	assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in content
	assert "scripts/write_codex_config.sh" in content
	assert "CODEX_HOME" in content


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
