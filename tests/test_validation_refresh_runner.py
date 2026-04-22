#!/usr/bin/env python3
"""Unit tests for scripts/validation_refresh_runner.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "validation_refresh_runner.py"

spec = importlib.util.spec_from_file_location("validation_refresh_runner", MODULE_PATH)
assert spec is not None and spec.loader is not None
refresh_runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = refresh_runner
spec.loader.exec_module(refresh_runner)


@dataclass
class PlannedCall:
	prefix: tuple[str, ...]
	stdout: str = ""
	stderr: str = ""
	returncode: int = 0
	check: bool = True
	callback: Callable[[list[str], Path | None], None] | None = None


class FakeExecutor:
	def __init__(self, calls: list[PlannedCall]) -> None:
		self._calls = calls
		self.seen: list[tuple[list[str], Path | None, bool, dict[str, str] | None]] = []

	def run(
		self,
		command: list[str],
		*,
		cwd: Path | None = None,
		check: bool = True,
		env_overrides: dict[str, str] | None = None,
	) -> subprocess.CompletedProcess[str]:
		self.seen.append((list(command), cwd, check, env_overrides))
		assert self._calls, f"unexpected command: {command}"
		plan = self._calls.pop(0)
		assert check == plan.check, f"expected check={plan.check}, got check={check} for {command}"
		assert tuple(command[: len(plan.prefix)]) == plan.prefix, (
			f"expected command prefix {plan.prefix}, got {tuple(command)}"
		)
		if plan.callback is not None:
			plan.callback(command, cwd)
		proc = subprocess.CompletedProcess(command, plan.returncode, stdout=plan.stdout, stderr=plan.stderr)
		if check and proc.returncode != 0:
			raise refresh_runner.CommandFailure(
				command=tuple(command),
				cwd=str(cwd) if cwd is not None else None,
				returncode=proc.returncode,
				stdout=proc.stdout,
				stderr=proc.stderr,
			)
		return proc

	def assert_consumed(self) -> None:
		assert not self._calls, f"unconsumed planned calls: {self._calls}"


def _write_manifest(repo_dir: Path) -> None:
	(repo_dir / ".ai").mkdir(parents=True, exist_ok=True)
	(repo_dir / ".ai" / "validate.yml").write_text(
		"""type: python-mongo-flask
entry: app.py
port: 8000
slots:
  project_name: demo
  canary_tools: [curl, jq]
  tap_plan: 2
""",
		encoding="utf-8",
	)


def test_load_target_repositories_deduplicates_and_validates() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-refresh-load-") as td:
		repos_path = Path(td) / "consumer_repos.json"
		repos_path.write_text(
			json.dumps(["owner-one/repo-one", "owner-one/repo-one", "owner-two/repo-two"]),
			encoding="utf-8",
		)

		repositories = refresh_runner.load_target_repositories(repos_path)
		assert repositories == ["owner-one/repo-one", "owner-two/repo-two"]

		repos_path.write_text(json.dumps(["owner/repo", 42]), encoding="utf-8")
		try:
			refresh_runner.load_target_repositories(repos_path)
		except ValueError as exc:
			assert "index 1" in str(exc)
		else:
			raise AssertionError("expected ValueError for non-string repository entry")

		repos_path.write_text("not-json", encoding="utf-8")
		try:
			refresh_runner.load_target_repositories(repos_path)
		except ValueError as exc:
			assert "not valid JSON" in str(exc)
		else:
			raise AssertionError("expected ValueError for malformed JSON")


def test_process_repository_green_existing_pr_promotes_and_enables_auto_merge() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-refresh-green-") as td:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)
		repository = "octo/demo-repo"
		repo_dir = workspace / "octo__demo-repo"
		branch = "ai/validation-refresh"

		def on_clone(_command: list[str], _cwd: Path | None) -> None:
			_write_manifest(repo_dir)

		executor = FakeExecutor(
			[
				PlannedCall(("gh", "repo", "view"), stdout="main\n"),
				PlannedCall(("gh", "repo", "clone"), callback=on_clone),
				PlannedCall(("git", "ls-remote"), stdout="abc refs/heads/ai/validation-refresh\n"),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "fetch", "origin", branch)),
				PlannedCall(("git", "checkout", "-B", branch, f"origin/{branch}")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=" M validation/tests/00_canary.sh\n"),
				PlannedCall(("git", "config", "user.name")),
				PlannedCall(("git", "config", "user.email")),
				PlannedCall(("gh", "auth", "setup-git")),
				PlannedCall(("git", "add", "-A")),
				PlannedCall(("git", "status"), stdout=" M validation/tests/00_canary.sh\n"),
				PlannedCall(("git", "commit", "-m")),
				PlannedCall(("git", "push", "--force-with-lease", "--set-upstream", "origin", branch)),
				PlannedCall(
					("gh", "pr", "list"),
					stdout='[{"number":42,"url":"https://github.com/octo/demo-repo/pull/42","isDraft":true}]',
				),
				PlannedCall(("gh", "pr", "edit", "42")),
				PlannedCall(("gh", "pr", "ready", "42"), check=False),
				PlannedCall(("gh", "pr", "merge", "42")),
			]
		)

		runner = refresh_runner.ValidationRefreshRunner(
			source_root=REPO_ROOT,
			branch_name=branch,
			commit_message="chore(validation): refresh validation assets",
			pr_title="chore(validation): refresh validation assets",
			executor=executor,
		)
		result = runner.process_repository(repository, workspace)

		assert result.outcome == "green"
		assert result.changed is True
		assert result.pr_number == 42
		assert result.pr_url == "https://github.com/octo/demo-repo/pull/42"
		assert result.diagnostics == []
		executor.assert_consumed()


def test_process_repository_red_new_draft_pr_with_diagnostics() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-refresh-red-") as td:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)
		repository = "octo/demo-repo"
		repo_dir = workspace / "octo__demo-repo"
		branch = "ai/validation-refresh"

		def on_clone(_command: list[str], _cwd: Path | None) -> None:
			_write_manifest(repo_dir)

		executor = FakeExecutor(
			[
				PlannedCall(("gh", "repo", "view"), stdout="main\n"),
				PlannedCall(("gh", "repo", "clone"), callback=on_clone),
				PlannedCall(("git", "ls-remote"), stdout=""),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(
					("python3", str(REPO_ROOT / "scripts" / "validation_lint.py")),
					returncode=1,
					stderr="lint failed",
				),
				PlannedCall(("git", "status"), stdout=" M validation/tests/00_canary.sh\n"),
				PlannedCall(("git", "config", "user.name")),
				PlannedCall(("git", "config", "user.email")),
				PlannedCall(("gh", "auth", "setup-git")),
				PlannedCall(("git", "add", "-A")),
				PlannedCall(("git", "status"), stdout=" M validation/tests/00_canary.sh\n"),
				PlannedCall(("git", "commit", "-m")),
				PlannedCall(("git", "push", "--force-with-lease", "--set-upstream", "origin", branch)),
				PlannedCall(("gh", "pr", "list"), stdout="[]"),
				PlannedCall(("gh", "pr", "create"), stdout="https://github.com/octo/demo-repo/pull/52\n"),
			]
		)

		runner = refresh_runner.ValidationRefreshRunner(
			source_root=REPO_ROOT,
			branch_name=branch,
			commit_message="chore(validation): refresh validation assets",
			pr_title="chore(validation): refresh validation assets",
			executor=executor,
		)
		result = runner.process_repository(repository, workspace)

		assert result.outcome == "red"
		assert result.changed is True
		assert result.pr_number == 52
		assert any("lint_failed" in line for line in result.diagnostics)
		create_commands = [cmd for cmd, _cwd, _check, _env in executor.seen if cmd[:3] == ["gh", "pr", "create"]]
		assert len(create_commands) == 1
		assert "--draft" in create_commands[0]
		executor.assert_consumed()


def test_process_repository_green_auto_merge_failure_falls_back_to_red() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-refresh-merge-") as td:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)
		repository = "octo/demo-repo"
		repo_dir = workspace / "octo__demo-repo"
		branch = "ai/validation-refresh"

		def on_clone(_command: list[str], _cwd: Path | None) -> None:
			_write_manifest(repo_dir)

		executor = FakeExecutor(
			[
				PlannedCall(("gh", "repo", "view"), stdout="main\n"),
				PlannedCall(("gh", "repo", "clone"), callback=on_clone),
				PlannedCall(("git", "ls-remote"), stdout=""),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=" M validation/tests/00_canary.sh\n"),
				PlannedCall(("git", "config", "user.name")),
				PlannedCall(("git", "config", "user.email")),
				PlannedCall(("gh", "auth", "setup-git")),
				PlannedCall(("git", "add", "-A")),
				PlannedCall(("git", "status"), stdout=" M validation/tests/00_canary.sh\n"),
				PlannedCall(("git", "commit", "-m")),
				PlannedCall(("git", "push", "--force-with-lease", "--set-upstream", "origin", branch)),
				PlannedCall(
					("gh", "pr", "list"),
					stdout='[{"number":77,"url":"https://github.com/octo/demo-repo/pull/77","isDraft":false}]',
				),
				PlannedCall(("gh", "pr", "edit", "77")),
				PlannedCall(("gh", "pr", "merge", "77"), returncode=1, stderr="auto-merge unavailable"),
				PlannedCall(("gh", "pr", "edit", "77")),
			]
		)

		runner = refresh_runner.ValidationRefreshRunner(
			source_root=REPO_ROOT,
			branch_name=branch,
			commit_message="chore(validation): refresh validation assets",
			pr_title="chore(validation): refresh validation assets",
			executor=executor,
		)
		result = runner.process_repository(repository, workspace)

		assert result.outcome == "red"
		assert result.pr_number == 77
		assert any("auto_merge_failed" in line for line in result.diagnostics)
		assert "auto_merge_enable_failed_fallback_to_draft" not in result.diagnostics
		assert result.diagnostics
		edit_commands = [cmd for cmd, _cwd, _check, _env in executor.seen if cmd[:3] == ["gh", "pr", "edit"]]
		assert len(edit_commands) == 2
		ready_commands = [cmd for cmd, _cwd, _check, _env in executor.seen if cmd[:3] == ["gh", "pr", "ready"]]
		assert len(ready_commands) == 0
		executor.assert_consumed()


def test_process_repository_green_new_pr_auto_merge_failure_falls_back_to_red_draft() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-refresh-new-merge-") as td:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)
		repository = "octo/demo-repo"
		repo_dir = workspace / "octo__demo-repo"
		branch = "ai/validation-refresh"

		def on_clone(_command: list[str], _cwd: Path | None) -> None:
			_write_manifest(repo_dir)

		executor = FakeExecutor(
			[
				PlannedCall(("gh", "repo", "view"), stdout="main\n"),
				PlannedCall(("gh", "repo", "clone"), callback=on_clone),
				PlannedCall(("git", "ls-remote"), stdout=""),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=" M validation/tests/00_canary.sh\n"),
				PlannedCall(("git", "config", "user.name")),
				PlannedCall(("git", "config", "user.email")),
				PlannedCall(("gh", "auth", "setup-git")),
				PlannedCall(("git", "add", "-A")),
				PlannedCall(("git", "status"), stdout=" M validation/tests/00_canary.sh\n"),
				PlannedCall(("git", "commit", "-m")),
				PlannedCall(("git", "push", "--force-with-lease", "--set-upstream", "origin", branch)),
				PlannedCall(("gh", "pr", "list"), stdout="[]"),
				PlannedCall(("gh", "pr", "create"), stdout="https://github.com/octo/demo-repo/pull/88\n"),
				PlannedCall(("gh", "pr", "merge", "88"), returncode=1, stderr="auto-merge unavailable"),
				PlannedCall(("gh", "pr", "ready", "--undo", "88"), check=False),
				PlannedCall(("gh", "pr", "edit", "88")),
			]
		)

		runner = refresh_runner.ValidationRefreshRunner(
			source_root=REPO_ROOT,
			branch_name=branch,
			commit_message="chore(validation): refresh validation assets",
			pr_title="chore(validation): refresh validation assets",
			executor=executor,
		)
		result = runner.process_repository(repository, workspace)

		assert result.outcome == "red"
		assert result.pr_number == 88
		assert result.pr_url == "https://github.com/octo/demo-repo/pull/88"
		assert "auto_merge_enable_failed_fallback_to_draft" in result.diagnostics
		ready_commands = [cmd for cmd, _cwd, _check, _env in executor.seen if cmd[:3] == ["gh", "pr", "ready"]]
		assert len(ready_commands) == 1
		executor.assert_consumed()


def test_process_repository_no_changes_skips_pr_operations() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-refresh-no-change-") as td:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)
		repository = "octo/demo-repo"
		repo_dir = workspace / "octo__demo-repo"
		branch = "ai/validation-refresh"

		def on_clone(_command: list[str], _cwd: Path | None) -> None:
			_write_manifest(repo_dir)

		executor = FakeExecutor(
			[
				PlannedCall(("gh", "repo", "view"), stdout="main\n"),
				PlannedCall(("gh", "repo", "clone"), callback=on_clone),
				PlannedCall(("git", "ls-remote"), stdout=""),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		runner = refresh_runner.ValidationRefreshRunner(
			source_root=REPO_ROOT,
			branch_name=branch,
			commit_message="chore(validation): refresh validation assets",
			pr_title="chore(validation): refresh validation assets",
			executor=executor,
		)
		result = runner.process_repository(repository, workspace)

		assert result.outcome == "skipped"
		assert result.changed is False
		assert "no_changes_detected" in result.diagnostics
		assert all(cmd[:2] != ["gh", "pr"] for cmd, _cwd, _check, _env in executor.seen)
		executor.assert_consumed()


def test_process_repository_pipeline_unsets_github_tokens() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-refresh-env-") as td:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)
		repository = "octo/demo-repo"
		repo_dir = workspace / "octo__demo-repo"
		branch = "ai/validation-refresh"

		def on_clone(_command: list[str], _cwd: Path | None) -> None:
			_write_manifest(repo_dir)

		executor = FakeExecutor(
			[
				PlannedCall(("gh", "repo", "view"), stdout="main\n"),
				PlannedCall(("gh", "repo", "clone"), callback=on_clone),
				PlannedCall(("git", "ls-remote"), stdout=""),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		runner = refresh_runner.ValidationRefreshRunner(
			source_root=REPO_ROOT,
			branch_name=branch,
			commit_message="chore(validation): refresh validation assets",
			pr_title="chore(validation): refresh validation assets",
			executor=executor,
		)
		runner.process_repository(repository, workspace)

		pipeline_commands = {
			str(REPO_ROOT / "scripts" / "render_validation_templates.py"),
			str(REPO_ROOT / "scripts" / "validation_lint.py"),
			str(REPO_ROOT / "scripts" / "validate_driver.sh"),
		}
		expected_log_dir = repo_dir.parent / f"{repo_dir.name}__validation_logs"
		for command, _cwd, _check, env_overrides in executor.seen:
			if any(item in command for item in pipeline_commands):
				assert env_overrides is not None
				assert env_overrides.get("GH_TOKEN") == ""
				assert env_overrides.get("GITHUB_TOKEN") == ""
				assert env_overrides.get("LOG_DIR") == str(expected_log_dir)
		executor.assert_consumed()


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
