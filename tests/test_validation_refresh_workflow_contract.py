#!/usr/bin/env python3
"""Contract tests for .github/workflows/validation-refresh.yml."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "validation-refresh.yml"
RELEASE_GATE_PATH = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"


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


def test_validation_refresh_workflow_bounds_discovery_with_budget() -> None:
	# Regression guard for runs 26733609724 / 26734252770: PR #3023 wired
	# OpenRouter auth so `codex exec` stopped failing fast and began doing real
	# per-repo work. With no aggregate budget the discovery phase ran 11 repos
	# x up to 3 attempts x 300s and tripped `timeout-minutes: 60`. Keep both
	# the budget knob and the cap it protects wired so this can't regress.
	content = WORKFLOW_PATH.read_text(encoding="utf-8")
	assert "VALIDATION_DISCOVERY_BUDGET_SECS" in content
	assert "timeout-minutes: 60" in content


def test_validation_refresh_workflow_wires_self_test_timeout() -> None:
	# The runner's 300s executor default is too short for large monorepos
	# (bitsafe.io exited 124); the workflow passes an explicit, overridable cap.
	content = WORKFLOW_PATH.read_text(encoding="utf-8")
	assert (
		"VALIDATION_REFRESH_SELF_TEST_TIMEOUT_SECONDS: "
		"${{ vars.VALIDATION_REFRESH_SELF_TEST_TIMEOUT_SECONDS || '900' }}"
	) in content


def test_validation_refresh_workflow_summarizes_and_notifies_on_failure() -> None:
	content = WORKFLOW_PATH.read_text(encoding="utf-8")
	assert "GITHUB_STEP_SUMMARY" in content
	assert "Telegram failure notification" in content
	assert "if: failure()" in content


# ---------------------------------------------------------------------------
# Release gate smoke dispatch (test-and-mark-stable.yml, orphan-workflows-test)
# ---------------------------------------------------------------------------
# Run 39694107256: the gate watched the dispatched validation-refresh run for
# only 2400s while that run's own `refresh` job may take 60 minutes, so a
# healthy, still-running refresh failed the stable release as "timed out".


def _job_block(text: str, job: str) -> str:
	match = re.search(rf"^  {re.escape(job)}:\n", text, re.MULTILINE)
	assert match, f"job {job!r} not found"
	rest = text[match.end():]
	end = re.search(r"^  [A-Za-z0-9_-]+:\n", rest, re.MULTILINE)
	return rest[: end.start()] if end else rest


def _job_header_timeout(block: str) -> int:
	match = re.search(r"^    timeout-minutes: (\d+)$", block, re.MULTILINE)
	assert match, "job-level timeout-minutes not found"
	return int(match.group(1))


def _step_block(job_block: str, step_name: str) -> str:
	marker = f"      - name: {step_name}\n"
	start = job_block.find(marker)
	assert start >= 0, f"step {step_name!r} not found"
	rest = job_block[start + len(marker):]
	end = re.search(r"^      - name:", rest, re.MULTILINE)
	return rest[: end.start()] if end else rest


def _release_validation_refresh_step() -> str:
	text = RELEASE_GATE_PATH.read_text(encoding="utf-8")
	job = _job_block(text, "orphan-workflows-test")
	return _step_block(job, "Dispatch & watch — validation-refresh")


def test_release_gate_validation_refresh_smoke_skips_model_discovery() -> None:
	step = _release_validation_refresh_step()
	assert "--field discovery_dry_run=true" in step


def test_release_gate_validation_refresh_watch_covers_child_budget() -> None:
	child_minutes = _job_header_timeout(
		_job_block(WORKFLOW_PATH.read_text(encoding="utf-8"), "refresh")
	)
	release_text = RELEASE_GATE_PATH.read_text(encoding="utf-8")
	job = _job_block(release_text, "orphan-workflows-test")
	job_minutes = _job_header_timeout(job)
	step = _step_block(job, "Dispatch & watch — validation-refresh")

	watch = re.search(r"--completion-timeout-secs (\d+)", step)
	assert watch, "--completion-timeout-secs missing"
	watch_secs = int(watch.group(1))
	step_timeout = re.search(r"^        timeout-minutes: (\d+)$", step, re.MULTILINE)
	assert step_timeout, "step timeout-minutes missing"
	step_minutes = int(step_timeout.group(1))

	# The watcher's deadline counts from dispatch, so it must cover the child
	# job's whole budget plus queueing/registration slack.
	assert watch_secs >= child_minutes * 60 + 60, (watch_secs, child_minutes)
	# The watcher must report before GitHub kills the step ...
	assert watch_secs < step_minutes * 60, (watch_secs, step_minutes)
	# ... and the step before the job.
	assert step_minutes < job_minutes, (step_minutes, job_minutes)

	dispatch_steps = re.findall(
		r"^        id: dispatch_[a-z0-9_]+\n        timeout-minutes: (\d+)$",
		job,
		re.MULTILINE,
	)
	assert len(dispatch_steps) >= 3, dispatch_steps
	assert sum(int(m) for m in dispatch_steps) < job_minutes, (dispatch_steps, job_minutes)


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
