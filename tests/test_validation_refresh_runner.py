#!/usr/bin/env python3
"""Unit tests for scripts/validation_refresh_runner.py."""

from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
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
		input_text: str | None = None,
		timeout: int = 300,
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


def _disabled_discovery_ctx() -> "refresh_runner.discovery_module.DiscoveryRunContext":
	"""Return a DiscoveryRunContext with discovery disabled.

	Existing drift-monitoring tests assert exact `FakeExecutor` call
	sequences — leaving discovery enabled would inject extra calls and
	break unrelated assertions. Discovery is exercised separately by
	`test_validation_discovery_bootstrap.py` and the per-flow tests at
	the bottom of this file.
	"""

	return refresh_runner.discovery_module.DiscoveryRunContext(
		source_root=REPO_ROOT,
		prompt_path=REPO_ROOT / "prompts" / "mode-validate-discover.txt",
		schema_path=REPO_ROOT / "scripts" / "templates" / "slot_manifest.schema.json",
		stub_entry_script_source=REPO_ROOT
		/ "examples"
		/ "validation-fixtures"
		/ "run_validation_repo_checks.sh",
		codex_model="openai/gpt-5.4",
		codex_reasoning_effort="xhigh",
		codex_attempts=3,
		pr_branch_prefix="automation/validate-discovery",
		pr_label="automation:validate-bootstrap",
		dedup_days=7,
		enabled=False,
		dry_run=False,
	)


def _make_runner(
	executor: FakeExecutor,
	branch: str,
	*,
	discovery_ctx: "refresh_runner.discovery_module.DiscoveryRunContext | None" = None,
) -> "refresh_runner.ValidationRefreshRunner":
	return refresh_runner.ValidationRefreshRunner(
		source_root=REPO_ROOT,
		branch_name=branch,
		executor=executor,
		discovery_ctx=discovery_ctx if discovery_ctx is not None else _disabled_discovery_ctx(),
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


def test_process_repository_green_drift_records_no_push_diagnostic() -> None:
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
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=" M validation/tests/00_canary.sh\n"),
			]
		)

		runner = _make_runner(executor, branch)
		result = runner.process_repository(repository, workspace)

		assert result.outcome == "green"
		assert result.changed is True
		assert "validation_assets_drifted_no_push" in result.diagnostics
		assert result.pr_number is None
		assert result.pr_url is None
		# Confirm we never invoked any gh pr / git commit / git push commands,
		# and never consulted the remote `<branch_name>` ref (drift monitoring
		# always baselines on the default branch, ignoring any leftover refresh
		# branch from the previous PR-based flow).
		assert all(cmd[:2] != ["gh", "pr"] for cmd, _cwd, _check, _env in executor.seen)
		assert all(cmd[:2] != ["git", "commit"] for cmd, _cwd, _check, _env in executor.seen)
		assert all(cmd[:2] != ["git", "push"] for cmd, _cwd, _check, _env in executor.seen)
		assert all(cmd[:2] != ["git", "ls-remote"] for cmd, _cwd, _check, _env in executor.seen)
		assert not any(
			cmd[:3] == ["git", "fetch", "origin"] and len(cmd) > 3 and cmd[3] == branch
			for cmd, _cwd, _check, _env in executor.seen
		)
		executor.assert_consumed()


def test_process_repository_red_pipeline_failure_with_drift_records_no_push() -> None:
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
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(
					("python3", str(REPO_ROOT / "scripts" / "validation_lint.py")),
					returncode=1,
					stderr="lint failed",
				),
				PlannedCall(("git", "status"), stdout=" M validation/tests/00_canary.sh\n"),
			]
		)

		runner = _make_runner(executor, branch)
		result = runner.process_repository(repository, workspace)

		assert result.outcome == "red"
		assert result.changed is True
		assert any("lint_failed" in line for line in result.diagnostics)
		assert "validation_assets_drifted_no_push" in result.diagnostics
		assert result.pr_number is None
		assert all(cmd[:2] != ["gh", "pr"] for cmd, _cwd, _check, _env in executor.seen)
		assert all(cmd[:2] != ["git", "commit"] for cmd, _cwd, _check, _env in executor.seen)
		assert all(cmd[:2] != ["git", "push"] for cmd, _cwd, _check, _env in executor.seen)
		executor.assert_consumed()


def test_process_repository_pipeline_failure_no_drift_is_red_not_error() -> None:
	# Regression for release v1.14.0 (run 26994091117): consumer `digital_pa`
	# failed its self-test (app never became healthy) with NO asset drift and
	# was mis-classified `error`, which fails the runner and blocked the stable
	# release smoke. A consumer self-test failure is a `red` (non-blocking,
	# monitored) outcome whether or not the assets drifted — `error` is
	# reserved for genuine refresh-mechanism failures.
	with tempfile.TemporaryDirectory(prefix="validation-refresh-red-nodrift-") as td:
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
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(
					("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh")),
					returncode=1,
					stderr="self test failed",
				),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		runner = _make_runner(executor, branch)
		result = runner.process_repository(repository, workspace)

		assert result.outcome == "red"
		assert result.changed is False
		assert "pipeline_failed_without_changes" in result.diagnostics
		assert any("self_test_failed" in line for line in result.diagnostics)
		assert "validation_assets_drifted_no_push" not in result.diagnostics
		# `red` must not count toward the runner's exit-1 gate (error-only).
		assert refresh_runner.summarize_results([result])["totals"]["error"] == 0
		assert refresh_runner.summarize_results([result])["totals"]["red"] == 1
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
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		runner = _make_runner(executor, branch)
		result = runner.process_repository(repository, workspace)

		assert result.outcome == "skipped"
		assert result.changed is False
		assert "no_changes_detected" in result.diagnostics
		assert "validation_assets_drifted_no_push" not in result.diagnostics
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
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		runner = _make_runner(executor, branch)
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


def test_process_repository_bootstraps_manifest_for_manifestless_repo() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-refresh-bootstrap-") as td:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)
		repository = "octo/demo-repo"
		repo_dir = workspace / "octo__demo-repo"
		branch = "ai/validation-refresh"

		stub_manifest = (
			(REPO_ROOT / "examples" / "validation-fixtures" / "python-repo-checks.yml")
			.read_text(encoding="utf-8")
			.strip()
		)
		bootstrapped_untracked_status = "?? .ai/validate.yml\n?? scripts/run_validation_repo_checks.sh\n"

		def on_clone(_command: list[str], _cwd: Path | None) -> None:
			repo_dir.mkdir(parents=True, exist_ok=True)

		executor = FakeExecutor(
			[
				PlannedCall(("gh", "repo", "view"), stdout="main\n"),
				PlannedCall(("gh", "repo", "clone"), callback=on_clone),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=bootstrapped_untracked_status),
			]
		)

		runner = _make_runner(executor, branch)
		result = runner.process_repository(repository, workspace)

		assert result.outcome == "green"
		assert result.changed is True
		assert "validation_assets_drifted_no_push" in result.diagnostics
		assert result.pr_number is None
		assert "manifest_bootstrapped_from: examples/validation-fixtures/python-repo-checks.yml" in result.diagnostics
		assert "repo_check_entry_seeded: scripts/run_validation_repo_checks.sh" in result.diagnostics
		assert "no_changes_detected" not in result.diagnostics

		bootstrapped_manifest = (repo_dir / ".ai" / "validate.yml").read_text(encoding="utf-8").strip()
		assert bootstrapped_manifest == stub_manifest
		entry_script = repo_dir / "scripts" / "run_validation_repo_checks.sh"
		assert entry_script.is_file()
		assert os.access(entry_script, os.X_OK)
		# No commit/push/PR commands should be issued — onboarding stubs are
		# rendered into the temp clone for monitoring only; the consumer repo
		# pulls them in during its own validation flow.
		assert all(cmd[:2] != ["gh", "pr"] for cmd, _cwd, _check, _env in executor.seen)
		assert all(cmd[:2] != ["git", "commit"] for cmd, _cwd, _check, _env in executor.seen)
		assert all(cmd[:2] != ["git", "push"] for cmd, _cwd, _check, _env in executor.seen)
		executor.assert_consumed()


# ---------------------------------------------------------------------------
# Codex-driven discovery dispatch integration
# ---------------------------------------------------------------------------


VALID_NODE_RUNTIME_MANIFEST = """type: node-runtime
entry: npm
slots:
  project_name: demo-node
  canary_tools:
    - bash
    - node
    - npm
  tap_plan: 3
"""

VALID_PYTHON_REPO_CHECKS_MANIFEST = """type: python-repo-checks
entry: scripts/run_validation_repo_checks.sh
slots:
  project_name: demo-python
  canary_tools:
    - bash
    - python3
    - jq
  tap_plan: 3
"""



def _enabled_discovery_ctx(
	*, dry_run: bool = False
) -> "refresh_runner.discovery_module.DiscoveryRunContext":
	return refresh_runner.discovery_module.DiscoveryRunContext(
		source_root=REPO_ROOT,
		prompt_path=REPO_ROOT / "prompts" / "mode-validate-discover.txt",
		schema_path=REPO_ROOT / "scripts" / "templates" / "slot_manifest.schema.json",
		stub_entry_script_source=REPO_ROOT
		/ "examples"
		/ "validation-fixtures"
		/ "run_validation_repo_checks.sh",
		codex_model="openai/gpt-5.4",
		codex_reasoning_effort="xhigh",
		codex_attempts=3,
		pr_branch_prefix="automation/validate-discovery",
		pr_label="automation:validate-bootstrap",
		dedup_days=7,
		enabled=True,
		dry_run=dry_run,
	)


class _StubMemory:
	"""Monkey-patches `_dedup_skip` + `_append_discovery_memory` for integration tests.

	The bash-driven memory helpers require a live ai-memory branch which
	we cannot wire up in unit tests; we substitute callables that record
	the inputs they would have written so the assertions can check the
	dispatcher routed the right outcome.
	"""

	def __init__(self, *, dedup_returns: bool = False) -> None:
		self.dedup_returns = dedup_returns
		self.appended: list[dict[str, object]] = []
		self.dedup_calls: list[str] = []

	def install(self) -> None:
		self._orig_dedup = refresh_runner._dedup_skip
		self._orig_append = refresh_runner._append_discovery_memory

		def fake_dedup(**kwargs: object) -> bool:
			self.dedup_calls.append(str(kwargs.get("repository")))
			return self.dedup_returns

		def fake_append(**kwargs: object) -> None:
			self.appended.append(kwargs)

		refresh_runner._dedup_skip = fake_dedup  # type: ignore[assignment]
		refresh_runner._append_discovery_memory = fake_append  # type: ignore[assignment]

	def uninstall(self) -> None:
		refresh_runner._dedup_skip = self._orig_dedup  # type: ignore[assignment]
		refresh_runner._append_discovery_memory = self._orig_append  # type: ignore[assignment]

	def __enter__(self) -> "_StubMemory":
		self.install()
		return self

	def __exit__(self, *_exc) -> None:  # noqa: ANN002
		self.uninstall()


def test_discovery_dispatch_skips_when_dedup_hits() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-dedup-skip-") as td, _StubMemory(
		dedup_returns=True
	) as stub:
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
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		runner = _make_runner(executor, branch, discovery_ctx=_enabled_discovery_ctx())
		result = runner.process_repository(repository, workspace)

		assert result.discovery_outcome == "skipped_dedup"
		assert result.discovery_pr_url is None
		assert stub.appended == []  # dedup short-circuits before any memory write
		assert stub.dedup_calls == [repository]
		# No codex / push / PR calls should have been issued.
		assert all(cmd[0] != "codex" for cmd, _cwd, _check, _env in executor.seen)
		executor.assert_consumed()


def test_discovery_dispatch_opens_seed_pr_when_manifest_missing() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-seed-pr-") as td, _StubMemory() as stub:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)
		repository = "octo/demo-repo"
		repo_dir = workspace / "octo__demo-repo"
		branch = "ai/validation-refresh"

		def on_clone(_command: list[str], _cwd: Path | None) -> None:
			repo_dir.mkdir(parents=True, exist_ok=True)  # no .ai/validate.yml committed

		def on_codex(_command: list[str], _cwd: Path | None, _check, _env) -> None:
			return None

		executor = FakeExecutor(
			[
				PlannedCall(("gh", "repo", "view"), stdout="main\n"),
				PlannedCall(("gh", "repo", "clone"), callback=on_clone),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				# Discovery dispatch begins:
				PlannedCall(("git", "rev-parse", "HEAD"), stdout="abc123def4567890\n", check=False),
				PlannedCall(("codex",), stdout=VALID_NODE_RUNTIME_MANIFEST, check=False),
				PlannedCall(("gh", "pr", "list"), stdout="[]\n"),
				PlannedCall(("git", "checkout", "-B")),
				PlannedCall(("git", "config", "user.name")),
				PlannedCall(("git", "config", "user.email")),
				PlannedCall(("git", "add")),
				PlannedCall(("git", "commit")),
				PlannedCall(("git", "push", "-u", "origin")),
				PlannedCall(
					("gh", "pr", "create"),
					stdout="https://github.com/octo/demo-repo/pull/99\n",
				),
				# Tree reset after discovery:
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				# Drift monitoring pipeline (no manifest committed → bootstrap):
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		runner = _make_runner(executor, branch, discovery_ctx=_enabled_discovery_ctx())
		result = runner.process_repository(repository, workspace)

		assert result.discovery_outcome == "pr_opened"
		assert result.discovery_pr_url == "https://github.com/octo/demo-repo/pull/99"
		assert result.discovery_pr_branch is not None
		assert result.discovery_pr_branch.startswith("automation/validate-discovery/")
		assert len(stub.appended) == 1
		recorded = stub.appended[0]
		assert recorded["outcome"] == "success_seeded"
		assert recorded["discovered_type"] == "node-runtime"
		assert recorded["committed_type"] is None
		assert recorded["pr_url"] == "https://github.com/octo/demo-repo/pull/99"
		executor.assert_consumed()


def test_discovery_dispatch_reports_agree_when_types_match() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-agree-") as td, _StubMemory() as stub:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)
		repository = "octo/demo-repo"
		repo_dir = workspace / "octo__demo-repo"
		branch = "ai/validation-refresh"

		def on_clone(_command: list[str], _cwd: Path | None) -> None:
			repo_dir.mkdir(parents=True, exist_ok=True)
			(repo_dir / ".ai").mkdir(parents=True, exist_ok=True)
			(repo_dir / ".ai" / "validate.yml").write_text(VALID_NODE_RUNTIME_MANIFEST, encoding="utf-8")

		executor = FakeExecutor(
			[
				PlannedCall(("gh", "repo", "view"), stdout="main\n"),
				PlannedCall(("gh", "repo", "clone"), callback=on_clone),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("git", "rev-parse", "HEAD"), stdout="abc123def4567890\n", check=False),
				PlannedCall(("codex",), stdout=VALID_NODE_RUNTIME_MANIFEST, check=False),
				# No PR — agree path skips push and gh pr create.
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		runner = _make_runner(executor, branch, discovery_ctx=_enabled_discovery_ctx())
		result = runner.process_repository(repository, workspace)

		assert result.discovery_outcome == "agree"
		assert result.discovery_pr_url is None
		assert len(stub.appended) == 1
		recorded = stub.appended[0]
		assert recorded["outcome"] == "success_agree"
		assert recorded["committed_type"] == "node-runtime"
		assert recorded["discovered_type"] == "node-runtime"
		assert recorded["pr_url"] is None
		# No codex retry, no push.
		assert sum(1 for cmd, _cwd, _check, _env in executor.seen if cmd[0] == "codex") == 1
		assert all(cmd[:3] != ["git", "push", "-u"] for cmd, _cwd, _check, _env in executor.seen)
		executor.assert_consumed()


def test_discovery_dispatch_opens_disagree_pr_on_type_mismatch() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-disagree-") as td, _StubMemory() as stub:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)
		repository = "octo/demo-repo"
		repo_dir = workspace / "octo__demo-repo"
		branch = "ai/validation-refresh"

		def on_clone(_command: list[str], _cwd: Path | None) -> None:
			repo_dir.mkdir(parents=True, exist_ok=True)
			(repo_dir / ".ai").mkdir(parents=True, exist_ok=True)
			(repo_dir / ".ai" / "validate.yml").write_text(
				VALID_PYTHON_REPO_CHECKS_MANIFEST, encoding="utf-8"
			)

		executor = FakeExecutor(
			[
				PlannedCall(("gh", "repo", "view"), stdout="main\n"),
				PlannedCall(("gh", "repo", "clone"), callback=on_clone),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("git", "rev-parse", "HEAD"), stdout="abc123def4567890\n", check=False),
				PlannedCall(("codex",), stdout=VALID_NODE_RUNTIME_MANIFEST, check=False),  # discovery says node-runtime
				PlannedCall(("gh", "pr", "list"), stdout="[]\n"),
				PlannedCall(("git", "checkout", "-B")),
				PlannedCall(("git", "config", "user.name")),
				PlannedCall(("git", "config", "user.email")),
				PlannedCall(("git", "add")),
				PlannedCall(("git", "commit")),
				PlannedCall(("git", "push", "-u", "origin")),
				PlannedCall(
					("gh", "pr", "create"),
					stdout="https://github.com/octo/demo-repo/pull/101\n",
				),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		runner = _make_runner(executor, branch, discovery_ctx=_enabled_discovery_ctx())
		stdout = io.StringIO()
		with contextlib.redirect_stdout(stdout):
			result = runner.process_repository(repository, workspace)

		assert result.discovery_outcome == "pr_opened"
		assert result.discovery_pr_url == "https://github.com/octo/demo-repo/pull/101"
		assert (
			"VALIDATION_DISCOVERY_DISAGREE "
			"repository=octo/demo-repo committed_type=python-repo-checks discovered_type=node-runtime"
		) in stdout.getvalue()
		assert len(stub.appended) == 1
		recorded = stub.appended[0]
		assert recorded["outcome"] == "success_disagree"
		assert recorded["committed_type"] == "python-repo-checks"
		assert recorded["discovered_type"] == "node-runtime"
		executor.assert_consumed()


def test_discovery_dispatch_records_dry_run_outcome_without_codex() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-dry-run-") as td, _StubMemory() as stub:
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
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("git", "rev-parse", "HEAD"), stdout="abc123def4567890\n", check=False),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		runner = _make_runner(executor, branch, discovery_ctx=_enabled_discovery_ctx(dry_run=True))
		result = runner.process_repository(repository, workspace)

		assert result.discovery_outcome == "dry_run"
		assert result.discovery_pr_url is None
		assert len(stub.appended) == 1
		recorded = stub.appended[0]
		assert recorded["outcome"] == "dry_run"
		assert recorded["pr_url"] is None
		assert all(cmd[0] != "codex" for cmd, _cwd, _check, _env in executor.seen)
		executor.assert_consumed()


def test_discovery_dispatch_records_failed_when_codex_exhausts() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-failed-") as td, _StubMemory() as stub:
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
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("git", "rev-parse", "HEAD"), stdout="abc123def4567890\n", check=False),
				PlannedCall(("codex",), stdout="error: nope\n", check=False),
				PlannedCall(("codex",), stdout="error: still nope\n", check=False),
				PlannedCall(("codex",), stdout="error: never\n", check=False),
				PlannedCall(("git", "fetch", "origin", "main")),
				PlannedCall(("git", "checkout", "-B", branch, "origin/main")),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "render_validation_templates.py"))),
				PlannedCall(("python3", str(REPO_ROOT / "scripts" / "validation_lint.py"))),
				PlannedCall(("bash", str(REPO_ROOT / "scripts" / "validate_driver.sh"))),
				PlannedCall(("git", "status"), stdout=""),
			]
		)

		ctx = _enabled_discovery_ctx()
		# Keep the test fast by suppressing the discovery module's retry sleep.
		# The dispatcher path does not expose a sleep override, so patch the
		# shared `time.sleep` used by `discover_manifest_via_codex`.
		import time as _time

		orig_sleep = _time.sleep
		_time.sleep = lambda _n: None  # type: ignore[assignment]
		try:
			runner = _make_runner(executor, branch, discovery_ctx=ctx)
			result = runner.process_repository(repository, workspace)
		finally:
			_time.sleep = orig_sleep  # type: ignore[assignment]

		assert result.discovery_outcome == "failed"
		assert result.discovery_pr_url is None
		assert len(stub.appended) == 1
		recorded = stub.appended[0]
		assert recorded["outcome"] == "failed"
		assert recorded["failure_reason"] is not None
		executor.assert_consumed()


def test_dispatch_discovery_skips_codex_when_budget_exhausted() -> None:
	# When the aggregate discovery budget is exhausted, the per-repo gate must
	# skip codex entirely (no dedup, no codex, no memory write) and record
	# `skipped_budget` so drift monitoring still proceeds. This is the fix for
	# the validation-refresh job timing out (runs 26733609724 / 26734252770)
	# once real codex discovery ran across all 11 consumer repos.
	with tempfile.TemporaryDirectory(prefix="discovery-budget-") as td, _StubMemory() as stub:
		repo_dir = Path(td) / "octo__demo-repo"
		repo_dir.mkdir(parents=True, exist_ok=True)
		branch = "ai/validation-refresh"
		executor = FakeExecutor([])  # gate must short-circuit before any command
		runner = _make_runner(executor, branch, discovery_ctx=_enabled_discovery_ctx())
		runner._discovery_deadline = time.monotonic() - 1.0  # already exhausted
		result = refresh_runner.RefreshResult(
			repository="octo/demo-repo", outcome="error", branch=branch
		)
		runner._dispatch_discovery(
			result=result,
			repo_dir=repo_dir,
			repository="octo/demo-repo",
			default_branch="main",
		)
		assert result.discovery_outcome == "skipped_budget"
		assert result.discovery_pr_url is None
		assert any("discovery_budget_exhausted" in d for d in result.discovery_diagnostics)
		assert stub.dedup_calls == []  # gate short-circuits before the dedup check
		assert stub.appended == []  # ...and before any memory write
		executor.assert_consumed()  # no codex/git/gh calls were issued


def test_dispatch_discovery_runs_when_budget_remaining() -> None:
	# A healthy remaining budget must let the gate fall through to the normal
	# discovery path — proven here by reaching the dedup check, which we stub
	# to hit so no real codex/network call is made.
	with tempfile.TemporaryDirectory(prefix="discovery-budget-ok-") as td, _StubMemory(
		dedup_returns=True
	) as stub:
		repo_dir = Path(td) / "octo__demo-repo"
		repo_dir.mkdir(parents=True, exist_ok=True)
		branch = "ai/validation-refresh"
		executor = FakeExecutor([])  # dedup-hit short-circuits before codex
		runner = _make_runner(executor, branch, discovery_ctx=_enabled_discovery_ctx())
		runner._discovery_deadline = time.monotonic() + 100000.0  # plenty remaining
		result = refresh_runner.RefreshResult(
			repository="octo/demo-repo", outcome="error", branch=branch
		)
		runner._dispatch_discovery(
			result=result,
			repo_dir=repo_dir,
			repository="octo/demo-repo",
			default_branch="main",
		)
		assert result.discovery_outcome == "skipped_dedup"  # gate passed -> dedup ran
		assert stub.dedup_calls == ["octo/demo-repo"]
		executor.assert_consumed()


def test_run_repositories_arms_discovery_deadline_per_enablement() -> None:
	# `run_repositories` arms the aggregate deadline when discovery is enabled
	# with a positive budget, and leaves it disarmed otherwise.
	with tempfile.TemporaryDirectory(prefix="discovery-deadline-") as td:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)

		enabled = _make_runner(
			FakeExecutor([]), "ai/validation-refresh", discovery_ctx=_enabled_discovery_ctx()
		)
		assert enabled._discovery_deadline is None  # not armed until run_repositories
		enabled.run_repositories([], workspace)
		assert enabled._discovery_deadline is not None

		disabled = _make_runner(FakeExecutor([]), "ai/validation-refresh")  # discovery disabled
		disabled.run_repositories([], workspace)
		assert disabled._discovery_deadline is None


def test_run_repositories_keeps_deadline_disarmed_for_non_positive_budget_env() -> None:
	# `VALIDATION_DISCOVERY_BUDGET_SECS<=0` is the documented opt-out for the
	# aggregate budget gate, so the env parsing path must preserve that value
	# instead of clamping it back to the 2100s default.
	with tempfile.TemporaryDirectory(prefix="discovery-deadline-env-") as td:
		workspace = Path(td) / "work"
		workspace.mkdir(parents=True, exist_ok=True)

		previous_budget = os.environ.get("VALIDATION_DISCOVERY_BUDGET_SECS")
		try:
			for raw_value in ("0", "-7"):
				os.environ["VALIDATION_DISCOVERY_BUDGET_SECS"] = raw_value
				runner = refresh_runner.ValidationRefreshRunner(
					source_root=REPO_ROOT,
					branch_name="ai/validation-refresh",
					executor=FakeExecutor([]),
					discovery_ctx=refresh_runner._build_default_discovery_ctx(REPO_ROOT),
				)
				assert runner.discovery_ctx.discovery_budget_secs == int(raw_value)
				runner.run_repositories([], workspace)
				assert runner._discovery_deadline is None
		finally:
			if previous_budget is None:
				os.environ.pop("VALIDATION_DISCOVERY_BUDGET_SECS", None)
			else:
				os.environ["VALIDATION_DISCOVERY_BUDGET_SECS"] = previous_budget


def test_stdout_contract_emits_repo_result_lines_and_final_summary() -> None:
	results = [
		refresh_runner.RefreshResult(
			repository="octo/demo-green",
			outcome="green",
			branch="ai/validation-refresh",
			diagnostics=["validation_assets_drifted_no_push"],
			changed=True,
			discovery_outcome="skipped_budget",
		),
		refresh_runner.RefreshResult(
			repository="octo/demo-red",
			outcome="red",
			branch="ai/validation-refresh",
			diagnostics=["self_test_failed(exit=1): boom", "pipeline_failed_without_changes"],
			changed=False,
			discovery_outcome="agree",
		),
		refresh_runner.RefreshResult(
			repository="octo/demo-skipped",
			outcome="skipped",
			branch="ai/validation-refresh",
			diagnostics=["no_changes_detected"],
			changed=False,
			discovery_outcome="disabled",
		),
	]

	stdout = io.StringIO()
	with contextlib.redirect_stdout(stdout):
		for result in results:
			refresh_runner._emit_repo_result(result)
		summary = refresh_runner.summarize_results(results)
		refresh_runner._emit_summary_line(summary, results)
		print(json.dumps(summary, sort_keys=True))

	lines = stdout.getvalue().splitlines()
	assert lines[0] == (
		'VALIDATION_REPO_RESULT repository=octo/demo-green outcome=green changed=1 '
		'drifted=1 discovery_outcome=skipped_budget diagnostics=1 '
		'detail="validation_assets_drifted_no_push"'
	)
	assert lines[1] == (
		'VALIDATION_REPO_RESULT repository=octo/demo-red outcome=red changed=0 '
		'drifted=0 discovery_outcome=agree diagnostics=2 '
		'detail="pipeline_failed_without_changes"'
	)
	assert lines[2] == (
		'VALIDATION_REPO_RESULT repository=octo/demo-skipped outcome=skipped changed=0 '
		'drifted=0 discovery_outcome=disabled diagnostics=1 '
		'detail="no_changes_detected"'
	)
	assert lines[3] == (
		"VALIDATION_SUMMARY processed=3 green=1 red=1 skipped=1 error=0 budget_exhausted=1"
	)
	json_payload = json.loads(lines[4])
	assert set(json_payload["totals"].keys()) == {"processed", "green", "red", "skipped", "error"}
	assert json_payload["totals"] == {
		"processed": 3,
		"green": 1,
		"red": 1,
		"skipped": 1,
		"error": 0,
	}
	assert "budget_exhausted" not in json_payload["totals"]


def test_main_emits_summary_line_before_json_for_empty_repo_set() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-refresh-empty-main-") as td:
		repos_path = Path(td) / "consumer_repos.json"
		repos_path.write_text("[]\n", encoding="utf-8")
		stdout = io.StringIO()
		original_argv = sys.argv[:]
		try:
			sys.argv = [
				str(MODULE_PATH),
				"--repos-file",
				str(repos_path),
			]
			with contextlib.redirect_stdout(stdout):
				exit_code = refresh_runner.main()
		finally:
			sys.argv = original_argv

		assert exit_code == 0
		lines = stdout.getvalue().splitlines()
		assert lines[0] == (
			"VALIDATION_SUMMARY processed=0 green=0 red=0 skipped=0 error=0 budget_exhausted=0"
		)
		assert json.loads(lines[1])["totals"] == {
			"processed": 0,
			"green": 0,
			"red": 0,
			"skipped": 0,
			"error": 0,
		}


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
