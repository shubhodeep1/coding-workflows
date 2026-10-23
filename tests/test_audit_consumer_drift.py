#!/usr/bin/env python3
"""Unit tests for scripts/audit_consumer_drift.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
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


def _parse_full_profile_manifest(manifest_path: Path) -> set[str]:
	names: set[str] = set()
	for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
		name = raw_line.strip()
		if name and not name.startswith("#"):
			names.add(name)
	return names


def test_expected_templates_include_every_full_profile_wrapper() -> None:
	templates_dir = REPO_ROOT / "workflow-templates"
	manifest_names = _parse_full_profile_manifest(templates_dir / "profiles" / "full.txt")
	expected_templates = drift_audit.load_expected_templates(templates_dir, CURRENT_STABLE_SHA)

	assert set(expected_templates) == manifest_names
	for dispatch_name in ("review_rb_judge_dispatch.yml", "unblock_judge_dispatch.yml"):
		assert dispatch_name in expected_templates
		rendered = expected_templates[dispatch_name]
		assert f"@{CURRENT_STABLE_SHA} # stable" in rendered
		assert ".yml@stable" not in rendered


def test_expected_templates_without_profile_manifest_load_only_ai_glob() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-no-manifest-") as td:
		templates_dir = Path(td) / "workflow-templates"
		_write_templates(
			templates_dir,
			{
				"ai-clarify.yml": "name: AI Clarify\n",
				"review_rb_judge_dispatch.yml": "name: Review-blocked judge dispatch\n",
			},
		)

		expected_templates = drift_audit.load_expected_templates(templates_dir)
		assert set(expected_templates) == {"ai-clarify.yml"}


def test_profile_manifest_ignores_missing_and_path_entries() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-manifest-") as td:
		templates_dir = Path(td) / "workflow-templates"
		_write_templates(
			templates_dir,
			{
				"ai-clarify.yml": "name: AI Clarify\n",
				"unblock_judge_dispatch.yml": "name: Unblock judge dispatch\n",
				"notes.txt": "not a workflow\n",
			},
		)
		(templates_dir / "sub").mkdir()
		(templates_dir / "sub" / "nested.yml").write_text("name: Nested\n", encoding="utf-8")
		(Path(td) / "escape.yml").write_text("name: Escape\n", encoding="utf-8")
		(templates_dir / "profiles").mkdir()
		(templates_dir / "profiles" / "full.txt").write_text(
			"# full profile\n"
			"\n"
			"ai-clarify.yml\n"
			"unblock_judge_dispatch.yml\n"
			"ghost.yml\n"
			"sub/nested.yml\n"
			"../escape.yml\n"
			"notes.txt\n",
			encoding="utf-8",
		)

		expected_templates = drift_audit.load_expected_templates(templates_dir)
		assert set(expected_templates) == {"ai-clarify.yml", "unblock_judge_dispatch.yml"}


def test_unreadable_profile_manifest_falls_back_to_ai_glob() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-bad-manifest-") as td:
		templates_dir = Path(td) / "workflow-templates"
		_write_templates(
			templates_dir,
			{
				"ai-clarify.yml": "name: AI Clarify\n",
				"unblock_judge_dispatch.yml": "name: Unblock judge dispatch\n",
			},
		)
		# A directory where the manifest file should be cannot be read as text.
		(templates_dir / "profiles" / "full.txt").mkdir(parents=True)

		expected_templates = drift_audit.load_expected_templates(templates_dir)
		assert set(expected_templates) == {"ai-clarify.yml"}


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


def _variables_call(repo: str, **kwargs) -> PlannedCall:
	return PlannedCall(
		(
			"gh",
			"api",
			"-H",
			"Accept: application/vnd.github+json",
			f"repos/{repo}/actions/variables/WORKFLOW_PROFILE",
		),
		**kwargs,
	)


def _listing_call(repo: str, names: list[str]) -> PlannedCall:
	return PlannedCall(
		(
			"gh",
			"api",
			"-H",
			"Accept: application/vnd.github+json",
			f"repos/{repo}/contents/.github/workflows",
		),
		stdout=json.dumps([{"name": name, "type": "file"} for name in names]),
	)


def _content_call(repo: str, name: str, body: str) -> PlannedCall:
	return PlannedCall(
		(
			"gh",
			"api",
			"-H",
			"Accept: application/vnd.github.raw+json",
			f"repos/{repo}/contents/.github/workflows/{name}",
		),
		stdout=body,
	)


PROFILE_TEMPLATE_BODIES = {
	"ai-clarify.yml": "name: AI Clarify\n",
	"ai-plan.yml": "name: AI Plan\n",
	"ai-orchestrate.yml": "name: AI Orchestrate\n",
	"ai-security-audit.yml": "name: AI Security Audit\n",
	"review_rb_judge_dispatch.yml": "name: Review-blocked judge dispatch\n",
}
PROFILE_MANIFESTS_TEXT = {
	"core": "ai-clarify.yml\nai-plan.yml\n",
	"standard": "ai-clarify.yml\nai-plan.yml\nai-orchestrate.yml\nreview_rb_judge_dispatch.yml\n",
	"full": "ai-clarify.yml\nai-plan.yml\nai-orchestrate.yml\nai-security-audit.yml\nreview_rb_judge_dispatch.yml\n",
}


def _profile_templates_dir(root: Path, *, skip_profiles: tuple[str, ...] = ()) -> Path:
	templates_dir = root / "workflow-templates"
	_write_templates(templates_dir, PROFILE_TEMPLATE_BODIES)
	(templates_dir / "profiles").mkdir()
	for profile, text in PROFILE_MANIFESTS_TEXT.items():
		if profile in skip_profiles:
			continue
		(templates_dir / "profiles" / f"{profile}.txt").write_text(text, encoding="utf-8")
	return templates_dir


def _run_profile_audit(
	templates_dir: Path,
	calls: list[PlannedCall],
	repo: str = "octo/demo",
) -> tuple[object, str, FakeExecutor]:
	executor = FakeExecutor(calls)
	auditor = drift_audit.ConsumerDriftAuditor(
		expected_templates=drift_audit.load_expected_templates(templates_dir),
		max_diff_lines=10,
		executor=executor,
		sleeper=lambda _delay: None,
		profile_manifests=drift_audit.load_profile_manifests(templates_dir),
	)
	buffer = io.StringIO()
	with contextlib.redirect_stdout(buffer):
		result = auditor.audit_repository(repo)
	executor.assert_consumed()
	return result, buffer.getvalue(), executor


def _missing_files(result) -> set[str]:
	return {item.file for item in result.drift_items if item.missing}


def test_core_profile_skips_out_of_profile_wrappers() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-core-") as td:
		templates_dir = _profile_templates_dir(Path(td))
		result, output, executor = _run_profile_audit(
			templates_dir,
			[
				_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "core"})),
				_listing_call("octo/demo", ["ai-clarify.yml"]),
				_content_call("octo/demo", "ai-clarify.yml", "name: AI Clarify\n"),
			],
		)
		assert result.outcome == "drift"
		assert _missing_files(result) == {"ai-plan.yml"}
		assert "profile=core manifest=loaded" in output.splitlines()[0]
		for name in ("ai-orchestrate.yml", "ai-security-audit.yml", "review_rb_judge_dispatch.yml"):
			assert (
				f"DRIFT_SCAN_SKIP repository=octo/demo file={name} reason=not_in_profile profile=core"
				in output
			)
		assert not any(
			"ai-orchestrate.yml" in command[-1] for command, _cwd, _check, _env in executor.seen
		)


def test_standard_profile_audits_review_dispatch_and_skips_full_only() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-standard-") as td:
		templates_dir = _profile_templates_dir(Path(td))
		result, output, _executor = _run_profile_audit(
			templates_dir,
			[
				_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "standard"})),
				_listing_call("octo/demo", ["ai-clarify.yml", "ai-plan.yml", "ai-orchestrate.yml"]),
				_content_call("octo/demo", "ai-clarify.yml", "name: AI Clarify\n"),
				_content_call("octo/demo", "ai-orchestrate.yml", "name: AI Orchestrate\n"),
				_content_call("octo/demo", "ai-plan.yml", "name: AI Plan\n"),
			],
		)
		assert _missing_files(result) == {"review_rb_judge_dispatch.yml"}
		assert "file=ai-security-audit.yml reason=not_in_profile profile=standard" in output
		assert "file=review_rb_judge_dispatch.yml reason=not_in_profile" not in output


def test_full_profile_and_unset_variable_report_every_missing_wrapper() -> None:
	for variable_call in (
		_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "full"})),
		_variables_call("octo/demo", stderr="gh: Not Found (HTTP 404)", returncode=1),
	):
		with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-full-") as td:
			templates_dir = _profile_templates_dir(Path(td))
			result, output, _executor = _run_profile_audit(
				templates_dir,
				[variable_call, _listing_call("octo/demo", [])],
			)
			assert _missing_files(result) == set(PROFILE_TEMPLATE_BODIES)
			assert "profile=full" in output.splitlines()[0]
			assert "DRIFT_SCAN_SKIP" not in output


def test_unknown_profile_keeps_previous_behaviour() -> None:
	for variable_call in (
		_variables_call("octo/demo", stderr="gh: Forbidden (HTTP 403)", returncode=1),
		_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "bogus"})),
		_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "../../core"})),
		# update_workflows.yml uses the raw value, so padded or mis-cased values
		# break its sync; the audit must not resolve them to a profile.
		_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": " Core "})),
		_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "core\n"})),
		_variables_call("octo/demo", stdout="not json"),
		_variables_call("octo/demo", stdout=json.dumps(["core"])),
	):
		with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-unknown-") as td:
			templates_dir = _profile_templates_dir(Path(td))
			result, output, _executor = _run_profile_audit(
				templates_dir,
				[variable_call, _listing_call("octo/demo", [])],
			)
			assert _missing_files(result) == set(PROFILE_TEMPLATE_BODIES)
			assert "profile=unknown manifest=disabled" in output.splitlines()[0]
			assert "DRIFT_SCAN_SKIP" not in output


def test_transient_profile_failure_is_retried() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-profile-retry-") as td:
		templates_dir = _profile_templates_dir(Path(td))
		result, output, _executor = _run_profile_audit(
			templates_dir,
			[
				_variables_call("octo/demo", stderr="gh: Bad Gateway (HTTP 502)", returncode=1),
				_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "core"})),
				_listing_call("octo/demo", []),
			],
		)
		start_lines = [line for line in output.splitlines() if line.startswith("DRIFT_SCAN_START ")]
		assert start_lines and start_lines[0].endswith("profile=core manifest=loaded")
		assert "DRIFT_SCAN_RETRY" in output
		assert _missing_files(result) == {"ai-clarify.yml", "ai-plan.yml"}


def test_present_out_of_profile_wrapper_is_still_compared() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-present-") as td:
		templates_dir = _profile_templates_dir(Path(td))
		result, _output, _executor = _run_profile_audit(
			templates_dir,
			[
				_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "core"})),
				_listing_call("octo/demo", ["ai-clarify.yml", "ai-plan.yml", "ai-orchestrate.yml"]),
				_content_call("octo/demo", "ai-clarify.yml", "name: AI Clarify\n"),
				_content_call("octo/demo", "ai-orchestrate.yml", "name: Locally edited\n"),
				_content_call("octo/demo", "ai-plan.yml", "name: AI Plan\n"),
			],
		)
		assert result.outcome == "drift"
		assert [(item.file, item.missing) for item in result.drift_items] == [("ai-orchestrate.yml", False)]


def test_missing_profile_manifest_disables_skipping() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-no-core-") as td:
		templates_dir = _profile_templates_dir(Path(td), skip_profiles=("core",))
		assert "core" not in drift_audit.load_profile_manifests(templates_dir)
		result, output, _executor = _run_profile_audit(
			templates_dir,
			[
				_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "core"})),
				_listing_call("octo/demo", []),
			],
		)
		assert _missing_files(result) == set(PROFILE_TEMPLATE_BODIES)
		assert "profile=core manifest=missing" in output.splitlines()[0]
		assert "DRIFT_SCAN_SKIP" not in output


def test_empty_profile_manifest_disables_skipping() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-empty-core-") as td:
		templates_dir = _profile_templates_dir(Path(td))
		(templates_dir / "profiles" / "core.txt").write_text("# comment only\n\n", encoding="utf-8")
		assert "core" not in drift_audit.load_profile_manifests(templates_dir)
		result, output, _executor = _run_profile_audit(
			templates_dir,
			[
				_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "core"})),
				_listing_call("octo/demo", []),
			],
		)
		assert _missing_files(result) == set(PROFILE_TEMPLATE_BODIES)
		assert "profile=core manifest=missing" in output.splitlines()[0]
		assert "DRIFT_SCAN_SKIP" not in output


def test_profile_is_cached_per_repository() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-consumer-drift-cache-") as td:
		templates_dir = _profile_templates_dir(Path(td))
		executor = FakeExecutor(
			[
				_variables_call("octo/demo", stdout=json.dumps({"name": "WORKFLOW_PROFILE", "value": "core"})),
				_listing_call("octo/demo", []),
			]
		)
		auditor = drift_audit.ConsumerDriftAuditor(
			expected_templates=drift_audit.load_expected_templates(templates_dir),
			max_diff_lines=10,
			executor=executor,
			profile_manifests=drift_audit.load_profile_manifests(templates_dir),
		)
		with contextlib.redirect_stdout(io.StringIO()):
			auditor.audit_repository("octo/demo")
			auditor.audit_repository("octo/demo")
		executor.assert_consumed()


def test_default_auditor_makes_no_profile_call() -> None:
	auditor = drift_audit.ConsumerDriftAuditor(
		expected_templates={},
		max_diff_lines=1,
		executor=FakeExecutor([]),
	)
	assert auditor.fetch_workflow_profile("octo/demo") == "unknown"


def test_load_profile_manifests_reads_repo_manifests() -> None:
	manifests = drift_audit.load_profile_manifests(REPO_ROOT / "workflow-templates")
	assert set(manifests) == {"core", "standard", "full"}
	assert "review_rb_judge_dispatch.yml" in manifests["standard"]
	assert "review_rb_judge_dispatch.yml" in manifests["full"]
	assert "review_rb_judge_dispatch.yml" not in manifests["core"]
	assert manifests["full"] == _parse_full_profile_manifest(
		REPO_ROOT / "workflow-templates" / "profiles" / "full.txt"
	)
	assert drift_audit._load_named_profile_manifest_names(REPO_ROOT / "workflow-templates", "../full") is None


def main() -> int:
	test_load_target_repositories_deduplicates_and_validates()
	test_expected_templates_render_current_stable_sha_and_detect_stale_pin()
	test_expected_templates_include_every_full_profile_wrapper()
	test_expected_templates_without_profile_manifest_load_only_ai_glob()
	test_profile_manifest_ignores_missing_and_path_entries()
	test_unreadable_profile_manifest_falls_back_to_ai_glob()
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
	test_core_profile_skips_out_of_profile_wrappers()
	test_standard_profile_audits_review_dispatch_and_skips_full_only()
	test_full_profile_and_unset_variable_report_every_missing_wrapper()
	test_unknown_profile_keeps_previous_behaviour()
	test_transient_profile_failure_is_retried()
	test_present_out_of_profile_wrapper_is_still_compared()
	test_missing_profile_manifest_disables_skipping()
	test_empty_profile_manifest_disables_skipping()
	test_profile_is_cached_per_repository()
	test_default_auditor_makes_no_profile_call()
	test_load_profile_manifests_reads_repo_manifests()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
