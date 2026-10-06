#!/usr/bin/env python3
"""Contract: the release gate installs pytest before its hot orchestrate-poll guard.

Run 37513567927 (issue #6591) failed in "Phase 0a: Hot orchestrate-poll
regression guard" with `ModuleNotFoundError: No module named 'pytest'`: the
guard runs `main`'s tests/test_orchestrate_poll_process.py, which imports
pytest, while the job's only pytest install sat much later in Phase 4b.
"""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

GUARD_ID = "hot-poller-guard"
DEPS_ID = "hot-poller-guard-deps"


def _steps() -> list[dict]:
	data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	return data["jobs"]["e2e-smoke-test"]["steps"]


def _index_by_id(steps: list[dict], step_id: str) -> int:
	matches = [i for i, step in enumerate(steps) if step.get("id") == step_id]
	assert len(matches) == 1, f"expected exactly one step with id {step_id!r}, found {len(matches)}"
	return matches[0]


def test_pytest_installed_before_hot_poller_guard() -> None:
	steps = _steps()
	deps_idx = _index_by_id(steps, DEPS_ID)
	guard_idx = _index_by_id(steps, GUARD_ID)
	assert deps_idx < guard_idx, (deps_idx, guard_idx)
	deps = steps[deps_idx]
	run = deps.get("run", "")
	assert "pip install --quiet pytest" in run, run
	assert "import pytest" in run, run
	assert "exit 1" in run, run
	assert "if" not in deps, deps
	assert "continue-on-error" not in deps, deps


def test_hot_poller_guard_unchanged() -> None:
	steps = _steps()
	guard = steps[_index_by_id(steps, GUARD_ID)]
	run = guard.get("run", "")
	assert (
		"tests/test_orchestrate_poll_process.py "
		"test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs"
	) in run, run
	assert "status=success" in run, run
	assert "status=exit_code=" in run, run
	assert "continue-on-error" not in guard, guard


def test_no_setup_python_before_hot_poller_guard() -> None:
	steps = _steps()
	guard_idx = _index_by_id(steps, GUARD_ID)
	for step in steps[:guard_idx]:
		assert "actions/setup-python" not in str(step.get("uses", "")), step


def test_install_follows_main_checkout() -> None:
	steps = _steps()
	deps_idx = _index_by_id(steps, DEPS_ID)
	checkout = [
		i
		for i, step in enumerate(steps)
		if str(step.get("name", "")).startswith("Checkout repository (early")
	]
	assert len(checkout) == 1, checkout
	assert checkout[0] < deps_idx, (checkout, deps_idx)


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
