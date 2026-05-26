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
					("codex",),
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
				PlannedRun(("codex",), stdout="error: cannot infer\n"),  # rejected
				PlannedRun(("codex",), stdout=VALID_NODE_RUNTIME_MANIFEST),  # accepted
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
				PlannedRun(("codex",), stdout="error: nope\n"),
				PlannedRun(("codex",), stdout="error: still nope\n"),
				PlannedRun(("codex",), stdout="error: never\n"),
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
		executor = FakeExecutor(
			[
				PlannedRun(
					("gh", "pr", "list"),
					stdout='[{"url":"https://github.com/octo/demo/pull/17","number":17}]\n',
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
