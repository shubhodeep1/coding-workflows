#!/usr/bin/env python3
"""Unit tests for scripts/validation_discovery_bootstrap.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parent.parent
# Discovery launches Codex through the credential-free, network-isolated
# helper (scripts/codex_isolated_exec.sh, read-only snapshot of the clone).
ISOLATED_CODEX_PREFIX = ("bash", str(REPO_ROOT / "scripts" / "codex_isolated_exec.sh"), "run", "--mode", "read-only")
MODULE_PATH = REPO_ROOT / "scripts" / "validation_discovery_bootstrap.py"

spec = importlib.util.spec_from_file_location("validation_discovery_bootstrap", MODULE_PATH)
assert spec is not None and spec.loader is not None
discovery = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = discovery
spec.loader.exec_module(discovery)


SCHEMA_PATH = REPO_ROOT / "scripts" / "templates" / "slot_manifest.schema.json"
PROMPT_PATH = REPO_ROOT / "prompts" / "mode-validate-discover.txt"


@dataclass
class PlannedRun:
	command_prefix: tuple[str, ...]
	stdout: str = ""
	stderr: str = ""
	returncode: int = 0
	callback: Callable[[list[str], Path | None, str | None], None] | None = None


class FakeExecutor:
	def __init__(self, planned: list[PlannedRun]) -> None:
		self._planned = planned
		self.seen: list[tuple[list[str], Path | None, str | None]] = []

	def run(
		self,
		command: list[str],
		*,
		cwd: Path | None = None,
		check: bool = True,
		env_overrides: dict[str, str] | None = None,
		input_text: str | None = None,
		timeout: int = 600,
	) -> subprocess.CompletedProcess[str]:
		self.seen.append((list(command), cwd, input_text))
		assert self._planned, f"unexpected command: {command}"
		plan = self._planned.pop(0)
		assert tuple(command[: len(plan.command_prefix)]) == plan.command_prefix, (
			f"expected prefix {plan.command_prefix}, got {tuple(command)}"
		)
		if plan.callback is not None:
			plan.callback(command, cwd, input_text)
		proc = subprocess.CompletedProcess(
			args=command,
			returncode=plan.returncode,
			stdout=plan.stdout,
			stderr=plan.stderr,
		)
		if check and proc.returncode != 0:
			raise subprocess.CalledProcessError(
				returncode=proc.returncode, cmd=command, output=plan.stdout, stderr=plan.stderr
			)
		return proc

	def assert_consumed(self) -> None:
		assert not self._planned, f"unconsumed: {self._planned}"


class CommandFailure(Exception):
	def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
		super().__init__(stderr or stdout or f"rc={returncode}")
		self.returncode = returncode
		self.stdout = stdout
		self.output = stdout
		self.stderr = stderr


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


def test_module_import_is_lazy_for_yaml_dependency() -> None:
	assert "yaml" not in discovery.__dict__


def test_validate_discovered_manifest_yaml_accepts_node_runtime() -> None:
	ok, parsed, reason = discovery.validate_discovered_manifest_yaml(
		VALID_NODE_RUNTIME_MANIFEST, schema_path=SCHEMA_PATH
	)
	assert ok is True
	assert reason is None
	assert parsed is not None and parsed["type"] == "node-runtime"


def test_validate_discovered_manifest_yaml_rejects_unknown_type() -> None:
	bad = """type: ruby-on-rails
slots:
  project_name: x
  canary_tools: [bash]
"""
	ok, parsed, reason = discovery.validate_discovered_manifest_yaml(bad, schema_path=SCHEMA_PATH)
	assert ok is False
	assert parsed is None
	assert reason is not None and "unsupported_type" in reason


def test_validate_discovered_manifest_yaml_rejects_empty() -> None:
	ok, parsed, reason = discovery.validate_discovered_manifest_yaml("", schema_path=SCHEMA_PATH)
	assert ok is False
	assert parsed is None
	assert reason == "empty_output"


def test_validate_discovered_manifest_yaml_rejects_error_text() -> None:
	bad = "error: codex failed to discover anything useful\n"
	ok, _parsed, reason = discovery.validate_discovered_manifest_yaml(bad, schema_path=SCHEMA_PATH)
	assert ok is False
	assert reason == "looks_like_error_output"


def test_validate_discovered_manifest_yaml_rejects_json_payload() -> None:
	bad = '{"type":"node-runtime"}'
	ok, _parsed, reason = discovery.validate_discovered_manifest_yaml(bad, schema_path=SCHEMA_PATH)
	assert ok is False
	assert reason == "looks_like_json_not_yaml"


def test_classify_disagreement_returns_false_when_types_match() -> None:
	result = discovery.classify_disagreement(
		committed_yaml_text=VALID_NODE_RUNTIME_MANIFEST,
		discovered_yaml_text=VALID_NODE_RUNTIME_MANIFEST,
	)
	assert result.disagrees is False
	assert result.committed_type == "node-runtime"
	assert result.discovered_type == "node-runtime"


def test_classify_disagreement_returns_true_when_types_differ() -> None:
	result = discovery.classify_disagreement(
		committed_yaml_text=VALID_PYTHON_REPO_CHECKS_MANIFEST,
		discovered_yaml_text=VALID_NODE_RUNTIME_MANIFEST,
	)
	assert result.disagrees is True
	assert result.committed_type == "python-repo-checks"
	assert result.discovered_type == "node-runtime"


def test_classify_disagreement_returns_false_when_committed_missing() -> None:
	# Missing committed manifest is the "seed" path; caller handles it
	# explicitly before reaching classify_disagreement. The function itself
	# returns disagrees=False so it never accidentally double-fires a PR.
	result = discovery.classify_disagreement(
		committed_yaml_text=None,
		discovered_yaml_text=VALID_NODE_RUNTIME_MANIFEST,
	)
	assert result.disagrees is False
	assert result.committed_type is None
	assert result.discovered_type == "node-runtime"


def test_build_pr_branch_name_is_deterministic_and_slugged() -> None:
	name = discovery.build_pr_branch_name(
		prefix="automation/validate-discovery",
		consumer_head_sha="abc1234567890def",
		discovered_type="node-runtime",
	)
	assert name == "automation/validate-discovery/abc123456789/node-runtime"
	# Slashes in slugs survive, but other punctuation is normalized.
	name2 = discovery.build_pr_branch_name(
		prefix="automation/validate-discovery",
		consumer_head_sha="0" * 12,
		discovered_type="python-mongo-flask",
	)
	assert name2 == "automation/validate-discovery/000000000000/python-mongo-flask"


def test_discover_manifest_via_codex_success_first_attempt() -> None:
	def on_codex(_command: list[str], _cwd: Path | None, input_text: str | None) -> None:
		assert input_text is not None
		assert "node-runtime" in input_text  # the prompt now lists the family

	with tempfile.TemporaryDirectory(prefix="discovery-success-") as td:
		clone_dir = Path(td)
		executor = FakeExecutor(
			[
				PlannedRun(
					ISOLATED_CODEX_PREFIX,
					stdout=VALID_NODE_RUNTIME_MANIFEST,
					callback=on_codex,
				),
			]
		)
		result = discovery.discover_manifest_via_codex(
			clone_dir=clone_dir,
			prompt_path=PROMPT_PATH,
			schema_path=SCHEMA_PATH,
			model="openai/gpt-5.4",
			reasoning_effort="xhigh",
			attempts=3,
			executor=executor,
			sleep_fn=lambda _seconds: None,
		)
		assert result.outcome == "success"
		assert result.attempts_used == 1
		assert result.parsed_manifest is not None
		assert result.parsed_manifest["type"] == "node-runtime"
		executor.assert_consumed()


def test_discover_manifest_via_codex_retries_validator_rejection() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-retry-") as td:
		clone_dir = Path(td)
		executor = FakeExecutor(
			[
				PlannedRun(ISOLATED_CODEX_PREFIX, stdout="error: cannot infer\n"),  # rejected
				PlannedRun(ISOLATED_CODEX_PREFIX, stdout=VALID_NODE_RUNTIME_MANIFEST),  # accepted
			]
		)
		result = discovery.discover_manifest_via_codex(
			clone_dir=clone_dir,
			prompt_path=PROMPT_PATH,
			schema_path=SCHEMA_PATH,
			model="openai/gpt-5.4",
			reasoning_effort="xhigh",
			attempts=3,
			executor=executor,
			sleep_fn=lambda _seconds: None,
		)
		assert result.outcome == "success"
		assert result.attempts_used == 2
		executor.assert_consumed()


def test_discover_manifest_via_codex_exhausted_attempts_returns_failure() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-exhaust-") as td:
		clone_dir = Path(td)
		executor = FakeExecutor(
			[
				PlannedRun(ISOLATED_CODEX_PREFIX, stdout="error: nope\n"),
				PlannedRun(ISOLATED_CODEX_PREFIX, stdout="error: still nope\n"),
				PlannedRun(ISOLATED_CODEX_PREFIX, stdout="error: never\n"),
			]
		)
		result = discovery.discover_manifest_via_codex(
			clone_dir=clone_dir,
			prompt_path=PROMPT_PATH,
			schema_path=SCHEMA_PATH,
			model="openai/gpt-5.4",
			reasoning_effort="xhigh",
			attempts=3,
			executor=executor,
			sleep_fn=lambda _seconds: None,
		)
		assert result.outcome == "failed"
		assert result.attempts_used == 3
		assert result.failure_reason is not None
		assert "validator_rejected" in result.failure_reason
		executor.assert_consumed()


def test_discover_manifest_via_codex_cli_missing_does_not_loop() -> None:
	class MissingCliExecutor:
		def __init__(self) -> None:
			self.calls = 0

		def run(self, *_args, **_kwargs):  # noqa: ANN002 ANN003
			self.calls += 1
			raise FileNotFoundError("codex")

	exe = MissingCliExecutor()
	with tempfile.TemporaryDirectory(prefix="discovery-missing-cli-") as td:
		result = discovery.discover_manifest_via_codex(
			clone_dir=Path(td),
			prompt_path=PROMPT_PATH,
			schema_path=SCHEMA_PATH,
			model="openai/gpt-5.4",
			reasoning_effort="xhigh",
			attempts=3,
			executor=exe,
			sleep_fn=lambda _seconds: None,
		)
	assert result.outcome == "failed"
	assert result.failure_reason == "codex_cli_missing"
	assert result.attempts_used == 1  # exits after the first FileNotFoundError
	assert exe.calls == 1


def test_discover_manifest_via_codex_wrapped_missing_cli_does_not_loop() -> None:
	class MissingCliExecutor:
		def __init__(self) -> None:
			self.calls = 0

		def run(self, *_args, **_kwargs):  # noqa: ANN002 ANN003
			self.calls += 1
			raise CommandFailure(
				returncode=127,
				stderr="[Errno 2] No such file or directory: 'codex'",
			)

	exe = MissingCliExecutor()
	with tempfile.TemporaryDirectory(prefix="discovery-wrapped-missing-cli-") as td:
		result = discovery.discover_manifest_via_codex(
			clone_dir=Path(td),
			prompt_path=PROMPT_PATH,
			schema_path=SCHEMA_PATH,
			model="openai/gpt-5.4",
			reasoning_effort="xhigh",
			attempts=3,
			executor=exe,
			sleep_fn=lambda _seconds: None,
		)
	assert result.outcome == "failed"
	assert result.failure_reason == "codex_cli_missing"
	assert result.attempts_used == 1
	assert exe.calls == 1


def test_open_or_update_discovery_pr_creates_new_pr_when_none_open() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-pr-create-") as td:
		clone_dir = Path(td)

		def on_add(command: list[str], _cwd: Path | None, _input_text: str | None) -> None:
			assert command == ["git", "add", ".ai/validate.yml"]

		executor = FakeExecutor(
			[
				PlannedRun(("gh", "pr", "list"), stdout="[]\n"),
				PlannedRun(("git", "checkout", "-B")),
				PlannedRun(("git", "config", "user.name")),
				PlannedRun(("git", "config", "user.email")),
				PlannedRun(("git", "add"), callback=on_add),
				PlannedRun(("git", "commit")),
				PlannedRun(("git", "push", "-u", "origin")),
				PlannedRun(
					("gh", "pr", "create"),
					stdout="https://github.com/octo/demo/pull/42\n",
				),
			]
		)
		result = discovery.open_or_update_discovery_pr(
			consumer_slug="octo/demo",
			consumer_clone_dir=clone_dir,
			base_branch="main",
			pr_branch="automation/validate-discovery/abc123/node-runtime",
			manifest_yaml=VALID_NODE_RUNTIME_MANIFEST,
			entry_script_text=None,
			entry_script_relative_path="scripts/run_validation_repo_checks.sh",
			pr_title="chore(validation): seed",
			rationale_md="body",
			label="automation:validate-bootstrap",
			executor=executor,
		)
		assert result.pr_url == "https://github.com/octo/demo/pull/42"
		assert result.pr_was_reused is False
		assert result.failure_reason is None
		executor.assert_consumed()


def test_open_or_update_discovery_pr_reuses_existing_open_pr() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-pr-reuse-") as td:
		clone_dir = Path(td)

		def on_list(command: list[str], _cwd: Path | None, _input_text: str | None) -> None:
			assert "--base" in command
			assert command[command.index("--base") + 1] == "main"

		executor = FakeExecutor(
			[
				PlannedRun(
					("gh", "pr", "list"),
					stdout='[{"url":"https://github.com/octo/demo/pull/17","number":17}]\n',
					callback=on_list,
				),
			]
		)
		result = discovery.open_or_update_discovery_pr(
			consumer_slug="octo/demo",
			consumer_clone_dir=clone_dir,
			base_branch="main",
			pr_branch="automation/validate-discovery/abc123/node-runtime",
			manifest_yaml=VALID_NODE_RUNTIME_MANIFEST,
			entry_script_text=None,
			entry_script_relative_path="scripts/run_validation_repo_checks.sh",
			pr_title="chore(validation): seed",
			rationale_md="body",
			label="automation:validate-bootstrap",
			executor=executor,
		)
		assert result.pr_url == "https://github.com/octo/demo/pull/17"
		assert result.pr_was_reused is True
		assert result.failure_reason is None
		executor.assert_consumed()


def test_open_or_update_discovery_pr_records_push_denied_on_push_failure() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-pr-push-fail-") as td:
		clone_dir = Path(td)
		executor = FakeExecutor(
			[
				PlannedRun(("gh", "pr", "list"), stdout="[]\n"),
				PlannedRun(("git", "checkout", "-B")),
				PlannedRun(("git", "config", "user.name")),
				PlannedRun(("git", "config", "user.email")),
				PlannedRun(("git", "add")),
				PlannedRun(("git", "commit")),
				PlannedRun(
					("git", "push", "-u", "origin"),
					returncode=128,
					stderr="remote: Permission to octo/demo.git denied.\n",
				),
				PlannedRun(("gh", "pr", "list"), returncode=1, stderr="gh unavailable\n"),
			]
		)
		result = discovery.open_or_update_discovery_pr(
			consumer_slug="octo/demo",
			consumer_clone_dir=clone_dir,
			base_branch="main",
			pr_branch="automation/validate-discovery/abc123/node-runtime",
			manifest_yaml=VALID_NODE_RUNTIME_MANIFEST,
			entry_script_text=None,
			entry_script_relative_path="scripts/run_validation_repo_checks.sh",
			pr_title="chore(validation): seed",
			rationale_md="body",
			label="automation:validate-bootstrap",
			executor=executor,
		)
		assert result.pr_url is None
		assert result.pr_was_reused is False
		assert result.failure_reason is not None
		assert result.failure_reason.startswith("push_denied")
		executor.assert_consumed()


def test_open_or_update_discovery_pr_records_wrapped_push_denied_on_push_failure() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-pr-wrapped-push-fail-") as td:
		clone_dir = Path(td)

		def on_push(_command: list[str], _cwd: Path | None, _input_text: str | None) -> None:
			raise CommandFailure(
				returncode=128,
				stderr="remote: Permission to octo/demo.git denied.\n",
			)

		executor = FakeExecutor(
			[
				PlannedRun(("gh", "pr", "list"), stdout="[]\n"),
				PlannedRun(("git", "checkout", "-B")),
				PlannedRun(("git", "config", "user.name")),
				PlannedRun(("git", "config", "user.email")),
				PlannedRun(("git", "add")),
				PlannedRun(("git", "commit")),
				PlannedRun(("git", "push", "-u", "origin"), callback=on_push),
				PlannedRun(("gh", "pr", "list"), returncode=1, stderr="gh unavailable\n"),
			]
		)
		result = discovery.open_or_update_discovery_pr(
			consumer_slug="octo/demo",
			consumer_clone_dir=clone_dir,
			base_branch="main",
			pr_branch="automation/validate-discovery/abc123/node-runtime",
			manifest_yaml=VALID_NODE_RUNTIME_MANIFEST,
			entry_script_text=None,
			entry_script_relative_path="scripts/run_validation_repo_checks.sh",
			pr_title="chore(validation): seed",
			rationale_md="body",
			label="automation:validate-bootstrap",
			executor=executor,
		)
		assert result.pr_url is None
		assert result.pr_was_reused is False
		assert result.failure_reason is not None
		assert result.failure_reason.startswith("push_denied")
		executor.assert_consumed()


def test_open_or_update_discovery_pr_reuses_existing_pr_after_push_race() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-pr-push-race-") as td:
		clone_dir = Path(td)
		executor = FakeExecutor(
			[
				PlannedRun(("gh", "pr", "list"), stdout="[]\n"),
				PlannedRun(("git", "checkout", "-B")),
				PlannedRun(("git", "config", "user.name")),
				PlannedRun(("git", "config", "user.email")),
				PlannedRun(("git", "add")),
				PlannedRun(("git", "commit")),
				PlannedRun(
					("git", "push", "-u", "origin"),
					returncode=1,
					stderr="! [rejected] non-fast-forward\n",
				),
				PlannedRun(
					("gh", "pr", "list"),
					stdout='[{"url":"https://github.com/octo/demo/pull/55","number":55}]\n',
				),
			]
		)
		result = discovery.open_or_update_discovery_pr(
			consumer_slug="octo/demo",
			consumer_clone_dir=clone_dir,
			base_branch="main",
			pr_branch="automation/validate-discovery/abc123/node-runtime",
			manifest_yaml=VALID_NODE_RUNTIME_MANIFEST,
			entry_script_text=None,
			entry_script_relative_path="scripts/run_validation_repo_checks.sh",
			pr_title="chore(validation): seed",
			rationale_md="body",
			label="automation:validate-bootstrap",
			executor=executor,
		)
		assert result.pr_url == "https://github.com/octo/demo/pull/55"
		assert result.pr_was_reused is True
		assert result.failure_reason is None
		executor.assert_consumed()


def test_open_or_update_discovery_pr_reuses_existing_pr_after_create_race() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-pr-create-race-") as td:
		clone_dir = Path(td)
		executor = FakeExecutor(
			[
				PlannedRun(("gh", "pr", "list"), stdout="[]\n"),
				PlannedRun(("git", "checkout", "-B")),
				PlannedRun(("git", "config", "user.name")),
				PlannedRun(("git", "config", "user.email")),
				PlannedRun(("git", "add")),
				PlannedRun(("git", "commit")),
				PlannedRun(("git", "push", "-u", "origin")),
				PlannedRun(
					("gh", "pr", "create"),
					returncode=1,
					stderr="a pull request for branch already exists\n",
				),
				PlannedRun(
					("gh", "pr", "list"),
					stdout='[{"url":"https://github.com/octo/demo/pull/56","number":56}]\n',
				),
			]
		)
		result = discovery.open_or_update_discovery_pr(
			consumer_slug="octo/demo",
			consumer_clone_dir=clone_dir,
			base_branch="main",
			pr_branch="automation/validate-discovery/abc123/node-runtime",
			manifest_yaml=VALID_NODE_RUNTIME_MANIFEST,
			entry_script_text=None,
			entry_script_relative_path="scripts/run_validation_repo_checks.sh",
			pr_title="chore(validation): seed",
			rationale_md="body",
			label="automation:validate-bootstrap",
			executor=executor,
		)
		assert result.pr_url == "https://github.com/octo/demo/pull/56"
		assert result.pr_was_reused is True
		assert result.failure_reason is None
		executor.assert_consumed()


def test_open_or_update_discovery_pr_rejects_entry_script_path_outside_clone() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-pr-invalid-entry-path-") as td:
		clone_dir = Path(td)
		outside_path = clone_dir.parent / "escape.sh"
		executor = FakeExecutor(
			[
				PlannedRun(("gh", "pr", "list"), stdout="[]\n"),
				PlannedRun(("git", "checkout", "-B")),
			]
		)
		result = discovery.open_or_update_discovery_pr(
			consumer_slug="octo/demo",
			consumer_clone_dir=clone_dir,
			base_branch="main",
			pr_branch="automation/validate-discovery/abc123/node-runtime",
			manifest_yaml=VALID_NODE_RUNTIME_MANIFEST,
			entry_script_text="#!/usr/bin/env bash\nexit 0\n",
			entry_script_relative_path="../escape.sh",
			pr_title="chore(validation): seed",
			rationale_md="body",
			label="automation:validate-bootstrap",
			executor=executor,
		)
		assert result.pr_url is None
		assert result.pr_was_reused is False
		assert result.failure_reason == "git_stage_failed: invalid_entry_script_path"
		assert not outside_path.exists()
		executor.assert_consumed()


def test_open_or_update_discovery_pr_retries_without_label_when_label_missing() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-pr-label-fallback-") as td:
		clone_dir = Path(td)

		def on_retry(command: list[str], _cwd: Path | None, _input_text: str | None) -> None:
			assert "--label" not in command

		executor = FakeExecutor(
			[
				PlannedRun(("gh", "pr", "list"), stdout="[]\n"),
				PlannedRun(("git", "checkout", "-B")),
				PlannedRun(("git", "config", "user.name")),
				PlannedRun(("git", "config", "user.email")),
				PlannedRun(("git", "add")),
				PlannedRun(("git", "commit")),
				PlannedRun(("git", "push", "-u", "origin")),
				PlannedRun(
					("gh", "pr", "create"),
					returncode=1,
					stderr="could not add label 'automation:validate-bootstrap': not found\n",
				),
				PlannedRun(
					("gh", "pr", "create"),
					stdout="https://github.com/octo/demo/pull/43\n",
					callback=on_retry,
				),
			]
		)
		result = discovery.open_or_update_discovery_pr(
			consumer_slug="octo/demo",
			consumer_clone_dir=clone_dir,
			base_branch="main",
			pr_branch="automation/validate-discovery/abc123/node-runtime",
			manifest_yaml=VALID_NODE_RUNTIME_MANIFEST,
			entry_script_text=None,
			entry_script_relative_path="scripts/run_validation_repo_checks.sh",
			pr_title="chore(validation): seed",
			rationale_md="body",
			label="automation:validate-bootstrap",
			executor=executor,
		)
		assert result.pr_url == "https://github.com/octo/demo/pull/43"
		assert result.failure_reason is None
		executor.assert_consumed()


def test_open_or_update_discovery_pr_retries_without_label_on_wrapped_missing_label_error() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-pr-wrapped-label-fallback-") as td:
		clone_dir = Path(td)

		def on_retry(command: list[str], _cwd: Path | None, _input_text: str | None) -> None:
			assert "--label" not in command

		def on_label_failure(command: list[str], _cwd: Path | None, _input_text: str | None) -> None:
			assert "--label" in command
			raise CommandFailure(
				returncode=1,
				stderr="could not add label 'automation:validate-bootstrap': not found\n",
			)

		executor = FakeExecutor(
			[
				PlannedRun(("gh", "pr", "list"), stdout="[]\n"),
				PlannedRun(("git", "checkout", "-B")),
				PlannedRun(("git", "config", "user.name")),
				PlannedRun(("git", "config", "user.email")),
				PlannedRun(("git", "add")),
				PlannedRun(("git", "commit")),
				PlannedRun(("git", "push", "-u", "origin")),
				PlannedRun(("gh", "pr", "create"), callback=on_label_failure),
				PlannedRun(
					("gh", "pr", "create"),
					stdout="https://github.com/octo/demo/pull/44\n",
					callback=on_retry,
				),
			]
		)
		result = discovery.open_or_update_discovery_pr(
			consumer_slug="octo/demo",
			consumer_clone_dir=clone_dir,
			base_branch="main",
			pr_branch="automation/validate-discovery/abc123/node-runtime",
			manifest_yaml=VALID_NODE_RUNTIME_MANIFEST,
			entry_script_text=None,
			entry_script_relative_path="scripts/run_validation_repo_checks.sh",
			pr_title="chore(validation): seed",
			rationale_md="body",
			label="automation:validate-bootstrap",
			executor=executor,
		)
		assert result.pr_url == "https://github.com/octo/demo/pull/44"
		assert result.failure_reason is None
		executor.assert_consumed()


def test_open_or_update_discovery_pr_does_not_drop_label_on_generic_error() -> None:
	with tempfile.TemporaryDirectory(prefix="discovery-pr-label-generic-") as td:
		clone_dir = Path(td)
		executor = FakeExecutor(
			[
				PlannedRun(("gh", "pr", "list"), stdout="[]\n"),
				PlannedRun(("git", "checkout", "-B")),
				PlannedRun(("git", "config", "user.name")),
				PlannedRun(("git", "config", "user.email")),
				PlannedRun(("git", "add")),
				PlannedRun(("git", "commit")),
				PlannedRun(("git", "push", "-u", "origin")),
				PlannedRun(
					("gh", "pr", "create"),
					returncode=1,
					stderr="secondary rate limit while updating label metadata\n",
				),
				PlannedRun(("gh", "pr", "list"), returncode=1, stderr="gh unavailable\n"),
			]
		)
		result = discovery.open_or_update_discovery_pr(
			consumer_slug="octo/demo",
			consumer_clone_dir=clone_dir,
			base_branch="main",
			pr_branch="automation/validate-discovery/abc123/node-runtime",
			manifest_yaml=VALID_NODE_RUNTIME_MANIFEST,
			entry_script_text=None,
			entry_script_relative_path="scripts/run_validation_repo_checks.sh",
			pr_title="chore(validation): seed",
			rationale_md="body",
			label="automation:validate-bootstrap",
			executor=executor,
		)
		assert result.pr_url is None
		assert result.failure_reason is not None
		assert result.failure_reason.startswith("gh_pr_create_failed")
		executor.assert_consumed()


def test_build_pr_rationale_markdown_does_not_use_auto_close_keywords() -> None:
	body = discovery.build_pr_rationale_markdown(
		consumer_slug="octo/demo",
		committed_type=None,
		discovered_type="node-runtime",
		consumer_head_sha="abc123def456",
		codex_model="openai/gpt-5.4",
		reasoning_effort="xhigh",
		attempts_used=1,
		is_seed=True,
	)
	lowered = body.lower()
	# §19 hard rule — no auto-close keywords in PR bodies authored by AI.
	for forbidden in ("fixes #", "closes #", "resolves #", "fix #", "close #", "resolve #"):
		assert forbidden not in lowered, f"forbidden auto-close keyword found: {forbidden!r}"


def test_build_pr_rationale_markdown_describes_automatic_close() -> None:
	body = discovery.build_pr_rationale_markdown(
		consumer_slug="octo/demo",
		committed_type=None,
		discovered_type="node-runtime",
		consumer_head_sha="abc123def456",
		codex_model="openai/gpt-5.4",
		reasoning_effort="xhigh",
		attempts_used=1,
		is_seed=True,
	)
	assert "closes\nthis PR automatically when a newer discovery PR replaces it" in body
	assert "superseded automatically if discovery later reveals" not in body


# ---------------------------------------------------------------------------
# Superseded discovery PR sweep (btc_sweeper#196)
# ---------------------------------------------------------------------------

DISCOVERY_PREFIX = "automation/validate-discovery"


def _bot_commits() -> list[dict[str, object]]:
	return [{"oid": "a1", "authors": [{"name": "codex-bot", "email": "codex@users.noreply.github.com"}]}]


def _bot_view_json() -> str:
	import json as _json

	return _json.dumps({"commits": _bot_commits()})


def _human_commits() -> list[dict[str, object]]:
	return _bot_commits() + [{"oid": "b2", "authors": [{"name": "Some Person", "email": "p@example.com"}]}]


def _open_pr(number: int, branch: str, *, title: str, bot_only: bool = True) -> "discovery.OpenDiscoveryPr":
	return discovery.OpenDiscoveryPr(
		number=number,
		url=f"https://github.com/octo/demo/pull/{number}",
		head_branch=branch,
		title=title,
		bot_only=bot_only,
	)


SEED_TITLE = "chore(validation): seed .ai/validate.yml (type: python-mongo-repo-checks)"
DISAGREE_TITLE = "chore(validation): discovery proposes type change (python-repo-checks → node-runtime)"


def test_manifest_type_from_yaml_reads_type_or_none() -> None:
	assert discovery.manifest_type_from_yaml(VALID_NODE_RUNTIME_MANIFEST) == "node-runtime"
	assert discovery.manifest_type_from_yaml(None) is None
	assert discovery.manifest_type_from_yaml("") is None
	assert discovery.manifest_type_from_yaml("entry: x\n") is None


def test_list_open_discovery_prs_filters_prefix_and_flags_human_commits() -> None:
	import json as _json

	payload = [
		{
			"number": 196,
			"url": "https://github.com/octo/demo/pull/196",
			"headRefName": f"{DISCOVERY_PREFIX}/38b0e3d9fa3d/python-mongo-repo-checks",
			"title": SEED_TITLE,
			"commits": _bot_commits(),
		},
		{
			"number": 200,
			"url": "https://github.com/octo/demo/pull/200",
			"headRefName": f"{DISCOVERY_PREFIX}/1111aaaa2222/node-runtime",
			"title": DISAGREE_TITLE,
			"commits": _human_commits(),
		},
		{
			"number": 201,
			"url": "https://github.com/octo/demo/pull/201",
			"headRefName": f"{DISCOVERY_PREFIX}/3333bbbb4444/node-runtime",
			"title": DISAGREE_TITLE,
		},
		{
			"number": 7,
			"url": "https://github.com/octo/demo/pull/7",
			"headRefName": "feature/unrelated",
			"title": "feat: something",
			"commits": _bot_commits(),
		},
	]

	def on_list(command: list[str], _cwd: Path | None, _input_text: str | None) -> None:
		assert command[command.index("--repo") + 1] == "octo/demo"
		assert command[command.index("--base") + 1] == "main"
		assert command[command.index("--state") + 1] == "open"
		assert "commits" in command[command.index("--json") + 1].split(",")

	executor = FakeExecutor(
		[PlannedRun(("gh", "pr", "list"), stdout=_json.dumps(payload), callback=on_list)]
	)
	prs, error = discovery.list_open_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		executor=executor,
	)
	assert error is None
	assert [pr.number for pr in prs] == [196, 200, 201]
	assert [pr.bot_only for pr in prs] == [True, False, False]  # no commit data => not bot-only
	executor.assert_consumed()


def test_list_open_discovery_prs_reports_lookup_failure() -> None:
	executor = FakeExecutor(
		[PlannedRun(("gh", "pr", "list"), returncode=1, stderr="HTTP 502: bad gateway")]
	)
	prs, error = discovery.list_open_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		executor=executor,
	)
	assert prs == []
	assert error is not None and error.startswith("gh_pr_list_failed(rc=1)")
	executor.assert_consumed()


def test_close_superseded_closes_seed_pr_once_manifest_committed() -> None:
	# The btc_sweeper#196 shape: a seed PR still open after another seed of the
	# same manifest merged. It can only conflict, so the sweep closes it.
	seen_comments: list[str] = []

	def on_close(command: list[str], _cwd: Path | None, _input_text: str | None) -> None:
		assert command[:4] == ["gh", "pr", "close", "196"]
		assert command[command.index("--repo") + 1] == "octo/demo"
		seen_comments.append(command[command.index("--comment") + 1])

	executor = FakeExecutor(
		[PlannedRun(("gh", "pr", "view", "196"), stdout=_bot_view_json()), PlannedRun(("gh", "pr", "close"), callback=on_close)]
	)
	outcomes = discovery.close_superseded_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		open_prs=[
			_open_pr(196, f"{DISCOVERY_PREFIX}/38b0e3d9fa3d/python-mongo-repo-checks", title=SEED_TITLE),
			# Proposes a type other than the committed one: still useful, kept open.
			_open_pr(300, f"{DISCOVERY_PREFIX}/5555cccc6666/node-runtime", title=DISAGREE_TITLE),
		],
		committed_type="python-mongo-repo-checks",
		manifest_committed=True,
		executor=executor,
	)
	assert [(o.number, o.reason, o.outcome) for o in outcomes] == [
		(196, "manifest_already_committed", "closed")
	]
	assert len(seen_comments) == 1
	assert "already committed on `main`" in seen_comments[0]
	assert "<!-- validation-discovery-superseded:v1 reason=manifest_already_committed -->" in seen_comments[0]
	lowered = seen_comments[0].lower()
	for forbidden in ("fixes #", "closes #", "resolves #", "fix #", "close #", "resolve #"):
		assert forbidden not in lowered
	executor.assert_consumed()


def test_close_superseded_closes_type_change_pr_when_type_already_committed() -> None:
	executor = FakeExecutor(
		[PlannedRun(("gh", "pr", "view", "300"), stdout=_bot_view_json()), PlannedRun(("gh", "pr", "close", "300"))]
	)
	outcomes = discovery.close_superseded_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		open_prs=[_open_pr(300, f"{DISCOVERY_PREFIX}/5555cccc6666/node-runtime", title=DISAGREE_TITLE)],
		committed_type="node-runtime",
		manifest_committed=True,
		executor=executor,
	)
	assert [(o.number, o.reason, o.outcome) for o in outcomes] == [
		(300, "proposed_type_already_committed", "closed")
	]
	executor.assert_consumed()


def test_close_superseded_leaves_prs_alone_without_manifest_or_replacement() -> None:
	executor = FakeExecutor([])
	outcomes = discovery.close_superseded_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		open_prs=[_open_pr(196, f"{DISCOVERY_PREFIX}/38b0e3d9fa3d/python-mongo-repo-checks", title=SEED_TITLE)],
		committed_type=None,
		manifest_committed=False,
		executor=executor,
	)
	assert outcomes == []
	executor.assert_consumed()


def test_close_superseded_closes_older_prs_when_replacement_opened() -> None:
	new_branch = f"{DISCOVERY_PREFIX}/b86ae3360365/python-mongo-repo-checks"

	def on_close(command: list[str], _cwd: Path | None, _input_text: str | None) -> None:
		assert "https://github.com/octo/demo/pull/235" in command[command.index("--comment") + 1]

	executor = FakeExecutor(
		[PlannedRun(("gh", "pr", "view", "196"), stdout=_bot_view_json()), PlannedRun(("gh", "pr", "close", "196"), callback=on_close)]
	)
	outcomes = discovery.close_superseded_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		open_prs=[
			_open_pr(196, f"{DISCOVERY_PREFIX}/38b0e3d9fa3d/python-mongo-repo-checks", title=SEED_TITLE),
			_open_pr(235, new_branch, title=SEED_TITLE),  # the replacement itself (reused PR)
		],
		committed_type=None,
		manifest_committed=False,
		replacement_branch=new_branch,
		replacement_url="https://github.com/octo/demo/pull/235",
		executor=executor,
	)
	assert [(o.number, o.reason, o.outcome) for o in outcomes] == [
		(196, "superseded_by_newer_pr", "closed")
	]
	executor.assert_consumed()


def test_close_superseded_never_closes_pr_with_human_commits() -> None:
	executor = FakeExecutor([])
	outcomes = discovery.close_superseded_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		open_prs=[
			_open_pr(
				196,
				f"{DISCOVERY_PREFIX}/38b0e3d9fa3d/python-mongo-repo-checks",
				title=SEED_TITLE,
				bot_only=False,
			)
		],
		committed_type="python-mongo-repo-checks",
		manifest_committed=True,
		executor=executor,
	)
	assert [(o.number, o.outcome) for o in outcomes] == [(196, "kept_human_commits")]
	executor.assert_consumed()  # no gh pr close


def test_close_superseded_dry_run_issues_no_close() -> None:
	executor = FakeExecutor([])
	outcomes = discovery.close_superseded_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		open_prs=[_open_pr(196, f"{DISCOVERY_PREFIX}/38b0e3d9fa3d/python-mongo-repo-checks", title=SEED_TITLE)],
		committed_type="python-mongo-repo-checks",
		manifest_committed=True,
		dry_run=True,
		executor=executor,
	)
	assert [(o.number, o.outcome) for o in outcomes] == [(196, "dry_run")]
	executor.assert_consumed()


def test_close_superseded_records_close_failure_and_continues() -> None:
	executor = FakeExecutor(
		[
			PlannedRun(("gh", "pr", "view", "196"), stdout=_bot_view_json()),
			PlannedRun(("gh", "pr", "close", "196"), returncode=1, stderr="GraphQL: Could not close"),
			PlannedRun(("gh", "pr", "view", "197"), stdout=_bot_view_json()),
			PlannedRun(("gh", "pr", "close", "197")),
		]
	)
	outcomes = discovery.close_superseded_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		open_prs=[
			_open_pr(196, f"{DISCOVERY_PREFIX}/38b0e3d9fa3d/python-mongo-repo-checks", title=SEED_TITLE),
			_open_pr(197, f"{DISCOVERY_PREFIX}/9b67ecb668a4/python-mongo-repo-checks", title=SEED_TITLE),
		],
		committed_type="python-mongo-repo-checks",
		manifest_committed=True,
		executor=executor,
	)
	assert [(o.number, o.outcome) for o in outcomes] == [(196, "close_failed"), (197, "closed")]
	assert outcomes[0].detail is not None and "Could not close" in outcomes[0].detail
	executor.assert_consumed()


def test_close_superseded_rechecks_commits_before_closing() -> None:
	# A person pushed after the listing: the re-check keeps the PR open.
	import json as _json

	executor = FakeExecutor(
		[PlannedRun(("gh", "pr", "view", "196"), stdout=_json.dumps({"commits": _human_commits()}))]
	)
	outcomes = discovery.close_superseded_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		open_prs=[_open_pr(196, f"{DISCOVERY_PREFIX}/38b0e3d9fa3d/python-mongo-repo-checks", title=SEED_TITLE)],
		committed_type="python-mongo-repo-checks",
		manifest_committed=True,
		executor=executor,
	)
	assert [(o.number, o.outcome) for o in outcomes] == [(196, "kept_human_commits")]
	executor.assert_consumed()  # no gh pr close


def test_close_superseded_recheck_failure_closes_nothing() -> None:
	executor = FakeExecutor([PlannedRun(("gh", "pr", "view", "196"), returncode=1, stderr="HTTP 502")])
	outcomes = discovery.close_superseded_discovery_prs(
		consumer_slug="octo/demo",
		base_branch="main",
		pr_branch_prefix=DISCOVERY_PREFIX,
		open_prs=[_open_pr(196, f"{DISCOVERY_PREFIX}/38b0e3d9fa3d/python-mongo-repo-checks", title=SEED_TITLE)],
		committed_type="python-mongo-repo-checks",
		manifest_committed=True,
		executor=executor,
	)
	assert [(o.number, o.outcome) for o in outcomes] == [(196, "close_failed")]
	assert outcomes[0].detail is not None and outcomes[0].detail.startswith("recheck_failed")
	executor.assert_consumed()


def test_seed_title_prefix_matches_runner_title() -> None:
	runner_text = (REPO_ROOT / "scripts" / "validation_refresh_runner.py").read_text(encoding="utf-8")
	assert "{discovery_module.SEED_PR_TITLE_PREFIX} (type: " in runner_text


def main() -> int:
	test_funcs = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
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
