#!/usr/bin/env python3
"""Unit tests for scripts/audit_consumer_drift.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "audit_consumer_drift.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "audit_consumer_drift.yml"
CURRENT_STABLE_SHA = "0123456789abcdef0123456789abcdef01234567"
STALE_STABLE_SHA = "89abcdef0123456789abcdef0123456789abcdef"
sys.path.insert(0, str(MODULE_PATH.parent))

spec = importlib.util.spec_from_file_location("audit_consumer_drift", MODULE_PATH)
assert spec is not None and spec.loader is not None
drift_audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = drift_audit
spec.loader.exec_module(drift_audit)


STANDARD_HEADER = """# IMPORTANT: This file is managed by coding-workflows and may be overwritten
# automatically when upstream templates change. To opt out of auto-updates,
# set the ALLOW_WORKFLOW_EDITS repository variable to 'false'.
"""


@dataclass
class PlannedCall:
	prefix: tuple[str, ...]
	stdout: str = ""
	stderr: str = ""
	returncode: int = 0
	check: bool = False
	raises: Exception | None = None


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
		del input_text, timeout
		self.seen.append((list(command), cwd, check, env_overrides))
		assert self._calls, f"unexpected command: {command}"
		plan = self._calls.pop(0)
		assert check == plan.check, f"expected check={plan.check}, got check={check} for {command}"
		assert tuple(command[: len(plan.prefix)]) == plan.prefix, (
			f"expected command prefix {plan.prefix}, got {tuple(command)}"
		)
		if plan.raises is not None:
			raise plan.raises
		proc = subprocess.CompletedProcess(command, plan.returncode, stdout=plan.stdout, stderr=plan.stderr)
		if check and proc.returncode != 0:
			raise drift_audit.CommandFailure(
				command=tuple(command),
				cwd=str(cwd) if cwd is not None else None,
				returncode=proc.returncode,
				stdout=proc.stdout,
				stderr=proc.stderr,
			)
		return proc

	def assert_consumed(self) -> None:
		assert not self._calls, f"unconsumed planned calls: {self._calls}"


def _write_templates(templates_dir: Path, files: dict[str, str]) -> None:
	templates_dir.mkdir(parents=True, exist_ok=True)
	for file_name, body in files.items():
		(templates_dir / file_name).write_text(body, encoding="utf-8")


def _load_expected_templates(templates_dir: Path) -> dict[str, str]:
	return drift_audit.load_expected_templates(templates_dir)


def test_load_target_repositories_deduplicates_and_validates() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-load-") as td:
		repos_path = Path(td) / "consumer_repos.json"
		repos_path.write_text(
			json.dumps(["owner-one/repo-one", "owner-one/repo-one", "owner-two/repo-two"]),
			encoding="utf-8",
		)

		repositories = drift_audit.load_target_repositories(repos_path)
		assert repositories == ["owner-one/repo-one", "owner-two/repo-two"]

		repos_path.write_text(json.dumps(["owner/repo", 42]), encoding="utf-8")
		try:
			drift_audit.load_target_repositories(repos_path)
		except ValueError as exc:
			assert "index 1" in str(exc)
		else:
			raise AssertionError("expected ValueError for non-string repository entry")


def test_expected_templates_render_current_stable_sha_and_detect_stale_pin() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-pins-") as td:
		templates_dir = Path(td) / "workflow-templates"
		canonical_text = (
			"name: AI Clarify\n"
			"jobs:\n"
			"  call:\n"
			"    uses: shubhodeep1/coding-workflows/.github/workflows/clarify.yml@stable\n"
		)
		_write_templates(templates_dir, {"ai-clarify.yml": canonical_text})

		expected_templates = drift_audit.load_expected_templates(
			templates_dir,
			CURRENT_STABLE_SHA,
		)
		expected_text = expected_templates["ai-clarify.yml"]
		assert f"@{CURRENT_STABLE_SHA} # stable" in expected_text
		assert ".yml@stable" not in expected_text

		current_executor = FakeExecutor(
			[
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/current/contents/.github/workflows",
					),
					stdout=json.dumps([{"name": "ai-clarify.yml", "type": "file"}]),
				),
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github.raw+json",
						"repos/octo/current/contents/.github/workflows/ai-clarify.yml",
					),
					stdout=expected_text,
				),
			]
		)
		current_auditor = drift_audit.ConsumerDriftAuditor(
			expected_templates=expected_templates,
			max_diff_lines=10,
			executor=current_executor,
		)
		assert current_auditor.audit_repository("octo/current").outcome == "match"
		current_executor.assert_consumed()

		executor = FakeExecutor(
			[
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/demo/contents/.github/workflows",
					),
					stdout=json.dumps([{"name": "ai-clarify.yml", "type": "file"}]),
				),
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github.raw+json",
						"repos/octo/demo/contents/.github/workflows/ai-clarify.yml",
					),
					stdout=expected_text.replace(CURRENT_STABLE_SHA, STALE_STABLE_SHA),
				),
			]
		)
		auditor = drift_audit.ConsumerDriftAuditor(
			expected_templates=expected_templates,
			max_diff_lines=10,
			executor=executor,
		)
		result = auditor.audit_repository("octo/demo")
		assert result.outcome == "drift"
		assert CURRENT_STABLE_SHA in result.drift_items[0].diff_preview
		assert STALE_STABLE_SHA in result.drift_items[0].diff_preview
		executor.assert_consumed()


def test_workflow_resolves_and_passes_current_stable_sha() -> None:
	workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
	assert "git fetch --no-tags --depth=1 origin refs/tags/stable" in workflow_text
	assert "STABLE_SHA=\"$(git rev-parse 'FETCH_HEAD^{commit}')\"" in workflow_text
	assert '--stable-sha "${STABLE_SHA,,}"' in workflow_text


def test_clean_match_normalizes_managed_header_and_trailing_whitespace() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-match-") as td:
		templates_dir = Path(td) / "workflow-templates"
		_write_templates(
			templates_dir,
			{
				"ai-clarify.yml": STANDARD_HEADER + "name: AI Clarify\non:\n  workflow_dispatch: {}\n",
			},
		)

		executor = FakeExecutor(
			[
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/demo/contents/.github/workflows",
					),
					stdout=json.dumps([{"name": "ai-clarify.yml", "type": "file"}]),
				),
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github.raw+json",
						"repos/octo/demo/contents/.github/workflows/ai-clarify.yml",
					),
					stdout="name: AI Clarify  \r\non:\r\n  workflow_dispatch: {}  \r\n",
				),
			]
		)

		auditor = drift_audit.ConsumerDriftAuditor(
			expected_templates=_load_expected_templates(templates_dir),
			max_diff_lines=5,
			executor=executor,
		)
		result = auditor.audit_repository("octo/demo")

		assert result.outcome == "match"
		assert result.drift_items == []
		executor.assert_consumed()


def test_missing_wrapper_is_reported_as_drift_without_extra_fetch() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-missing-") as td:
		templates_dir = Path(td) / "workflow-templates"
		_write_templates(
			templates_dir,
			{
				"ai-clarify.yml": STANDARD_HEADER + "name: AI Clarify\non:\n  workflow_dispatch: {}\n",
				"ai-plan.yml": STANDARD_HEADER + "name: AI Plan\non:\n  workflow_dispatch: {}\n",
			},
		)

		executor = FakeExecutor(
			[
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/demo/contents/.github/workflows",
					),
					stdout=json.dumps([{"name": "ai-clarify.yml", "type": "file"}]),
				),
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github.raw+json",
						"repos/octo/demo/contents/.github/workflows/ai-clarify.yml",
					),
					stdout="name: AI Clarify\non:\n  workflow_dispatch: {}\n",
				),
			]
		)

		auditor = drift_audit.ConsumerDriftAuditor(
			expected_templates=_load_expected_templates(templates_dir),
			max_diff_lines=6,
			executor=executor,
		)
		result = auditor.audit_repository("octo/demo")

		assert result.outcome == "drift"
		assert len(result.drift_items) == 1
		assert result.drift_items[0].file == "ai-plan.yml"
		assert result.drift_items[0].missing is True
		assert "workflow-templates/ai-plan.yml" in result.drift_items[0].diff_preview
		assert not any(
			"repos/octo/demo/contents/.github/workflows/ai-plan.yml" in command[-1]
			for command, _cwd, _check, _env in executor.seen
		)
		executor.assert_consumed()


def test_multi_repo_drift_aggregates_into_summary() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-summary-") as td:
		templates_dir = Path(td) / "workflow-templates"
		_write_templates(
			templates_dir,
			{
				"ai-clarify.yml": STANDARD_HEADER + "name: AI Clarify\non:\n  workflow_dispatch: {}\n",
			},
		)

		executor = FakeExecutor(
			[
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/one/contents/.github/workflows",
					),
					stdout=json.dumps([{"name": "ai-clarify.yml", "type": "file"}]),
				),
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github.raw+json",
						"repos/octo/one/contents/.github/workflows/ai-clarify.yml",
					),
					stdout="name: Drifted Clarify\non:\n  workflow_dispatch: {}\n",
				),
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/two/contents/.github/workflows",
					),
					stderr="gh: Not Found (HTTP 404)",
					returncode=1,
				),
			]
		)

		auditor = drift_audit.ConsumerDriftAuditor(
			expected_templates=_load_expected_templates(templates_dir),
			max_diff_lines=4,
			executor=executor,
		)
		results = auditor.audit_repositories(["octo/one", "octo/two"])
		summary = drift_audit.summarize_results(results, ["ai-clarify.yml"])

		assert [result.outcome for result in results] == ["drift", "drift"]
		assert summary["totals"] == {
			"processed": 2,
			"match": 0,
			"drift": 2,
			"error": 0,
			"drift_items": 2,
		}
		assert not any(
			"repos/octo/two/contents/.github/workflows/ai-clarify.yml" in command[-1]
			for command, _cwd, _check, _env in executor.seen
		)
		executor.assert_consumed()


def test_repo_fetch_failure_is_recorded_after_exhausting_retries() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-error-") as td:
		templates_dir = Path(td) / "workflow-templates"
		_write_templates(
			templates_dir,
			{
				"ai-clarify.yml": STANDARD_HEADER + "name: AI Clarify\non:\n  workflow_dispatch: {}\n",
			},
		)

		broken_listing = PlannedCall(
			(
				"gh",
				"api",
				"-H",
				"Accept: application/vnd.github+json",
				"repos/octo/broken/contents/.github/workflows",
			),
			stderr="gh: upstream unavailable (HTTP 500)",
			returncode=1,
		)
		executor = FakeExecutor(
			[
				# A persistent transient 500 is retried up to the attempt ceiling
				# before the repository is finally recorded as errored.
				broken_listing,
				broken_listing,
				broken_listing,
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/healthy/contents/.github/workflows",
					),
					stdout=json.dumps([{"name": "ai-clarify.yml", "type": "file"}]),
				),
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github.raw+json",
						"repos/octo/healthy/contents/.github/workflows/ai-clarify.yml",
					),
					stdout="name: AI Clarify\non:\n  workflow_dispatch: {}\n",
				),
			]
		)

		auditor = drift_audit.ConsumerDriftAuditor(
			expected_templates=_load_expected_templates(templates_dir),
			max_diff_lines=5,
			executor=executor,
			gh_api_max_attempts=3,
			sleeper=lambda _delay: None,
		)
		results = auditor.audit_repositories(["octo/broken", "octo/healthy"])
		summary = drift_audit.summarize_results(results, ["ai-clarify.yml"])

		assert results[0].outcome == "error"
		assert results[0].error == "fetch_failed"
		assert "workflow directory listing failed" in (results[0].error_detail or "")
		assert results[1].outcome == "match"
		assert summary["totals"] == {
			"processed": 2,
			"match": 1,
			"drift": 0,
			"error": 1,
			"drift_items": 0,
		}
		broken_listing_calls = [
			command
			for command, _cwd, _check, _env in executor.seen
			if command[-1] == "repos/octo/broken/contents/.github/workflows"
		]
		assert len(broken_listing_calls) == 3
		executor.assert_consumed()


def test_transient_fetch_failure_recovers_after_retry() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-retry-") as td:
		templates_dir = Path(td) / "workflow-templates"
		_write_templates(
			templates_dir,
			{
				"ai-clarify.yml": STANDARD_HEADER + "name: AI Clarify\non:\n  workflow_dispatch: {}\n",
			},
		)

		executor = FakeExecutor(
			[
				# GitHub's transient HTML "Unicorn" 500 page on the first attempt…
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/flaky/contents/.github/workflows",
					),
					stdout="<!DOCTYPE html> <title>Unicorn! &middot; GitHub</title>",
					returncode=1,
				),
				# …succeeds on retry, so the repository must not be flagged as errored.
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/flaky/contents/.github/workflows",
					),
					stdout=json.dumps([{"name": "ai-clarify.yml", "type": "file"}]),
				),
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github.raw+json",
						"repos/octo/flaky/contents/.github/workflows/ai-clarify.yml",
					),
					stdout="name: AI Clarify\non:\n  workflow_dispatch: {}\n",
				),
			]
		)

		slept: list[float] = []
		auditor = drift_audit.ConsumerDriftAuditor(
			expected_templates=_load_expected_templates(templates_dir),
			max_diff_lines=5,
			executor=executor,
			gh_api_max_attempts=4,
			gh_api_retry_base_seconds=2.0,
			sleeper=slept.append,
		)
		result = auditor.audit_repository("octo/flaky")

		assert result.outcome == "match"
		assert result.drift_items == []
		assert slept == [2.0]
		executor.assert_consumed()


def test_transient_fetch_failure_classifier_is_specific_and_covers_proxy_errors() -> None:
	assert drift_audit._is_transient_fetch_failure(
		"",
		"gh: GET repos/octo/timeout-service/contents/.github/workflows: HTTP 403",
		1,
	) is False
	assert drift_audit._is_transient_fetch_failure(
		"",
		"gh: GET repos/octo/rate-limiter/contents/.github/workflows: HTTP 400",
		1,
	) is False
	assert drift_audit._is_transient_fetch_failure("", "gh: Bad Gateway", 1) is True
	assert drift_audit._is_transient_fetch_failure("", "gh: API rate limit exceeded for 192.0.2.1", 1) is True


def test_retry_parameters_are_normalized_and_retry_delay_is_capped() -> None:
	endpoint = "repos/octo/flaky/contents/.github/workflows"
	transient_failure = PlannedCall(
		(
			"gh",
			"api",
			"-H",
			"Accept: application/vnd.github+json",
			endpoint,
		),
		stderr="gh: upstream unavailable (HTTP 500)",
		returncode=1,
	)
	executor = FakeExecutor(
		[
			transient_failure,
			transient_failure,
			transient_failure,
			transient_failure,
		]
	)
	slept: list[float] = []
	auditor = drift_audit.ConsumerDriftAuditor(
		expected_templates={},
		max_diff_lines=1,
		executor=executor,
		gh_api_max_attempts=4.0,
		gh_api_retry_base_seconds=20.0,
		sleeper=slept.append,
	)

	assert auditor.gh_api_max_attempts == 4
	assert auditor.gh_api_retry_base_seconds == 20.0
	proc = auditor._run_gh_api(endpoint)

	assert proc.returncode == 1
	assert slept == [20.0, 30.0, 30.0]
	executor.assert_consumed()


def test_retry_parameters_fall_back_to_safe_defaults() -> None:
	auditor = drift_audit.ConsumerDriftAuditor(
		expected_templates={},
		max_diff_lines=1,
		executor=FakeExecutor([]),
		gh_api_max_attempts="not-a-number",
		gh_api_retry_base_seconds=-5,
	)

	assert auditor.gh_api_max_attempts == drift_audit.DEFAULT_GH_API_MAX_ATTEMPTS
	assert auditor.gh_api_retry_base_seconds == 0.0


def test_command_failure_is_recorded_without_aborting_other_repos() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-command-failure-") as td:
		templates_dir = Path(td) / "workflow-templates"
		_write_templates(
			templates_dir,
			{
				"ai-clarify.yml": STANDARD_HEADER + "name: AI Clarify\non:\n  workflow_dispatch: {}\n",
			},
		)

		executor = FakeExecutor(
			[
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/broken/contents/.github/workflows",
					),
					raises=drift_audit.CommandFailure(
						command=("gh", "api"),
						cwd=None,
						returncode=124,
						stdout="",
						stderr="timeout_expired",
					),
				),
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github+json",
						"repos/octo/healthy/contents/.github/workflows",
					),
					stdout=json.dumps([{"name": "ai-clarify.yml", "type": "file"}]),
				),
				PlannedCall(
					(
						"gh",
						"api",
						"-H",
						"Accept: application/vnd.github.raw+json",
						"repos/octo/healthy/contents/.github/workflows/ai-clarify.yml",
					),
					stdout="name: AI Clarify\non:\n  workflow_dispatch: {}\n",
				),
			]
		)

		auditor = drift_audit.ConsumerDriftAuditor(
			expected_templates=_load_expected_templates(templates_dir),
			max_diff_lines=5,
			executor=executor,
		)
		results = auditor.audit_repositories(["octo/broken", "octo/healthy"])
		summary = drift_audit.summarize_results(results, ["ai-clarify.yml"])

		assert results[0].outcome == "error"
		assert results[0].error == "command_failed"
		assert "timeout_expired" in (results[0].error_detail or "")
		assert results[1].outcome == "match"
		assert summary["totals"] == {
			"processed": 2,
			"match": 1,
			"drift": 0,
			"error": 1,
			"drift_items": 0,
		}
		executor.assert_consumed()


def main() -> int:
	test_load_target_repositories_deduplicates_and_validates()
	test_expected_templates_render_current_stable_sha_and_detect_stale_pin()
	test_workflow_resolves_and_passes_current_stable_sha()
	test_clean_match_normalizes_managed_header_and_trailing_whitespace()
	test_missing_wrapper_is_reported_as_drift_without_extra_fetch()
	test_multi_repo_drift_aggregates_into_summary()
	test_repo_fetch_failure_is_recorded_after_exhausting_retries()
	test_transient_fetch_failure_recovers_after_retry()
	test_transient_fetch_failure_classifier_is_specific_and_covers_proxy_errors()
	test_retry_parameters_are_normalized_and_retry_delay_is_capped()
	test_retry_parameters_fall_back_to_safe_defaults()
	test_command_failure_is_recorded_without_aborting_other_repos()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
