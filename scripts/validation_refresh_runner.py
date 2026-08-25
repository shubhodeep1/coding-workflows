#!/usr/bin/env python3
"""Render and self-test validation assets against consumer repositories.

Runs the validation template render + lint + self-test pipeline against each
configured consumer repo in a temporary clone for monitoring purposes only.
This runner deliberately does NOT commit, push, or open pull requests in the
consumer repositories — consumers are expected to render the validation assets
on demand inside their own validation flow. The summary reports drift so the
operator can see when consumer repos are out of sync.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
	from validation_template_bootstrap import bootstrap_validation_manifest
except ModuleNotFoundError:
	scripts_dir = Path(__file__).resolve().parent
	if str(scripts_dir) not in sys.path:
		sys.path.insert(0, str(scripts_dir))
	from validation_template_bootstrap import bootstrap_validation_manifest

try:
	import validation_discovery_bootstrap as discovery_module
except ModuleNotFoundError:
	scripts_dir = Path(__file__).resolve().parent
	if str(scripts_dir) not in sys.path:
		sys.path.insert(0, str(scripts_dir))
	import validation_discovery_bootstrap as discovery_module


REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Per-`codex exec` wall-clock cap (seconds) for the discovery dispatch. This
# is the unit the aggregate discovery budget (VALIDATION_DISCOVERY_BUDGET_SECS)
# reasons about: a single repo's discovery can consume at most
# `codex_attempts * DISCOVERY_CODEX_CALL_TIMEOUT_SECS`, so the runner only
# starts a repo's discovery while that much budget remains. Matches the
# CommandExecutor default so the explicit cap and the budget math agree.
DISCOVERY_CODEX_CALL_TIMEOUT_SECS = 300


def _env_bool(name: str, default: bool) -> bool:
	value = os.environ.get(name)
	if value is None:
		return default
	normalized = value.strip().lower()
	if normalized in ("1", "true", "yes", "on"):
		return True
	if normalized in ("0", "false", "no", "off", ""):
		return False
	return default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
	value = os.environ.get(name)
	if not value:
		return default
	try:
		parsed = int(value.strip())
	except ValueError:
		return default
	return parsed if parsed >= minimum else default


def _env_str(name: str, default: str) -> str:
	value = os.environ.get(name)
	if value is None or not value.strip():
		return default
	return value.strip()


@dataclass(frozen=True)
class CommandFailure(Exception):
	"""Raised when a command exits non-zero while check=True."""

	command: tuple[str, ...]
	cwd: str | None
	returncode: int
	stdout: str
	stderr: str

	def __str__(self) -> str:
		cwd_display = self.cwd or os.getcwd()
		command_display = " ".join(self.command)
		return (
			f"Command failed (exit={self.returncode}) in {cwd_display}: {command_display}\n"
			f"stdout:\n{self.stdout}\n"
			f"stderr:\n{self.stderr}"
		)


class CommandExecutor:
	"""Thin subprocess wrapper with structured failures.

	Accepts `input_text` and `timeout` so the discovery dispatch can route
	its codex invocation through the same executor instance the refresh
	pipeline uses, which keeps the FakeExecutor surface uniform in tests.
	"""

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
		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		if env_overrides:
			env.update(env_overrides)
		try:
			proc = subprocess.run(
				command,
				cwd=str(cwd) if cwd is not None else None,
				text=True,
				capture_output=True,
				check=False,
				env=env,
				input=input_text,
				timeout=timeout,
			)
		except subprocess.TimeoutExpired as exc:
			raise CommandFailure(
				command=tuple(command),
				cwd=str(cwd) if cwd is not None else None,
				returncode=124,
				stdout=exc.stdout if isinstance(exc.stdout, str) else "",
				stderr=exc.stderr if isinstance(exc.stderr, str) else "timeout_expired",
			) from exc
		except FileNotFoundError as exc:
			raise CommandFailure(
				command=tuple(command),
				cwd=str(cwd) if cwd is not None else None,
				returncode=127,
				stdout="",
				stderr=str(exc),
			) from exc
		if check and proc.returncode != 0:
			raise CommandFailure(
				command=tuple(command),
				cwd=str(cwd) if cwd is not None else None,
				returncode=proc.returncode,
				stdout=proc.stdout,
				stderr=proc.stderr,
			)
		return proc


@dataclass
class RefreshResult:
	repository: str
	outcome: str
	branch: str | None = None
	pr_number: int | None = None
	pr_url: str | None = None
	diagnostics: list[str] = field(default_factory=list)
	changed: bool = False
	# Codex-driven `.ai/validate.yml` discovery — populated independently of
	# the refresh pipeline outcome above. `None` means discovery was disabled
	# or did not run for this repo this cycle (e.g., 7-day dedup hit).
	discovery_outcome: str | None = None
	discovery_pr_url: str | None = None
	discovery_pr_branch: str | None = None
	discovery_diagnostics: list[str] = field(default_factory=list)


class ValidationRefreshRunner:
	"""Renders validation assets in a temp clone and reports drift; never pushes.

	Discovery dispatch (codex-driven `.ai/validate.yml` proposal PRs to
	consumer repos) is layered on top of this read-only drift pipeline and
	IS allowed to push branches + open PRs on the consumer repo when the
	caller passes `discovery_enabled=True` (default).
	"""

	def __init__(
		self,
		*,
		source_root: Path,
		branch_name: str,
		commit_message: str = "",
		pr_title: str = "",
		executor: CommandExecutor | None = None,
		discovery_ctx: "discovery_module.DiscoveryRunContext | None" = None,
	) -> None:
		self.source_root = source_root
		self.branch_name = branch_name
		# `commit_message` and `pr_title` are accepted for backward compatibility
		# with the previous PR-creating runner; this runner no longer commits,
		# pushes, or opens pull requests in the drift-monitoring pipeline.
		# Discovery dispatch (below) opens consumer-repo PRs on its own.
		self.commit_message = commit_message
		self.pr_title = pr_title
		self.executor = executor or CommandExecutor()
		self.discovery_ctx = discovery_ctx if discovery_ctx is not None else _build_default_discovery_ctx(source_root)
		# Aggregate discovery wall-clock deadline (monotonic seconds), set at
		# the start of `run_repositories`. None disables the per-repo budget
		# gate — e.g. direct `process_repository` calls in tests, or a
		# non-positive configured budget (explicit unbounded opt-out).
		self._discovery_deadline: float | None = None

	def run_repositories(self, repositories: list[str], workspace_root: Path) -> list[RefreshResult]:
		# Establish the aggregate discovery deadline for this cycle so the
		# codex dispatch phase cannot run away across many consumer repos and
		# trip the job's `timeout-minutes` cap. Discovery is gated per-repo
		# against this deadline and degrades to drift-monitoring only once the
		# budget is exhausted (the "discovery must never block drift
		# monitoring" contract — runner Q4:A).
		ctx = self.discovery_ctx
		budget = ctx.discovery_budget_secs if ctx is not None else 0
		self._discovery_deadline = (
			time.monotonic() + budget
			if ctx is not None and ctx.enabled and budget > 0
			else None
		)
		results: list[RefreshResult] = []
		for repository in repositories:
			try:
				results.append(self.process_repository(repository, workspace_root))
			except Exception as exc:  # pragma: no cover - defensive fail-open branch
				results.append(
					RefreshResult(
						repository=repository,
						outcome="error",
						diagnostics=[f"unexpected_exception: {exc}"],
					)
				)
		return results

	def process_repository(self, repository: str, workspace_root: Path) -> RefreshResult:
		result = RefreshResult(repository=repository, outcome="error", branch=self.branch_name)
		repo_dir = workspace_root / repository.replace("/", "__")

		if not REPO_NAME_RE.match(repository):
			result.diagnostics.append(f"invalid_repository_name: {repository}")
			return result

		default_branch, default_branch_diag = self._resolve_default_branch(repository)
		if default_branch_diag:
			result.diagnostics.append(default_branch_diag)

		try:
			self._clone_repository(repository, repo_dir)
			self._checkout_refresh_branch(repo_dir, default_branch)
		except CommandFailure as exc:
			result.diagnostics.append(_format_command_failure("checkout", exc))
			return result

		# Discovery dispatch runs FIRST so it sees the committed default-branch
		# state before any local bootstrap mutates the tree. It pushes its own
		# PR branch when applicable; the dispatch helper resets the tree to
		# `origin/<default>` before returning when (and only when) discovery
		# actually mutated the working state.
		if self.discovery_ctx is not None and self.discovery_ctx.enabled:
			try:
				self._dispatch_discovery(
					result=result,
					repo_dir=repo_dir,
					repository=repository,
					default_branch=default_branch,
				)
			except Exception as exc:  # pragma: no cover - defensive fail-open
				print(f"VALIDATION_DISCOVERY_FAILED repository={repository} error={exc!r}")
				result.discovery_outcome = "failed"
				result.discovery_diagnostics.append(f"dispatch_uncaught: {exc!r}")

			# Discovery may have switched the working tree to a PR branch
			# whose `.ai/validate.yml` was overwritten with the discovered
			# content. Reset the tree to the committed default-branch state
			# so the drift pipeline below renders against the committed
			# inputs, not against the discovery proposal.
			if result.discovery_outcome in ("pr_opened", "pr_reused", "failed", "push_denied"):
				try:
					self._checkout_refresh_branch(repo_dir, default_branch)
				except CommandFailure as exc:
					result.diagnostics.append(
						_format_command_failure("post_discovery_checkout", exc)
					)
					return result
		else:
			result.discovery_outcome = "disabled"
			print(f"VALIDATION_DISCOVERY_SKIPPED_DISABLED repository={repository}")

		manifest_path = repo_dir / ".ai" / "validate.yml"
		if not manifest_path.is_file():
			try:
				bootstrap = bootstrap_validation_manifest(source_root=self.source_root, workspace_root=repo_dir)
			except (OSError, FileNotFoundError) as exc:
				result.outcome = "error"
				result.diagnostics.append(f"manifest_bootstrap_failed: {exc}")
				return result
			manifest_path = bootstrap.manifest_path
			result.diagnostics.extend(bootstrap.diagnostics)

		pipeline_green, diagnostics = self._run_refresh_pipeline(repo_dir, manifest_path)
		result.diagnostics.extend(diagnostics)

		has_changes = self._repository_has_changes(repo_dir)
		result.changed = has_changes

		if not has_changes:
			if pipeline_green:
				result.outcome = "skipped"
				result.diagnostics.append("no_changes_detected")
			else:
				# Refresh pipeline failed (render, lint, or self_test) and the
				# rendered assets matched the committed ones (no drift). This is
				# the same class of signal as the drift case below (`red`): a
				# consumer-repo pipeline health failure, NOT a refresh-mechanism
				# error. Classify it `red` (monitored, non-blocking) so it is
				# consistent with the drift-present path and does not fail the
				# runner. `error` stays reserved for genuine mechanism failures
				# (clone/checkout/manifest bootstrap/unexpected exceptions)
				# that DO gate the release smoke via main()'s exit code.
				# Regression: release v1.14.0 (run
				# 26994091117) was blocked when consumer `digital_pa` failed its
				# self-test (app never became healthy) with no drift and was
				# mis-classified `error`.
				result.outcome = "red"
				result.diagnostics.append("pipeline_failed_without_changes")
			return result

		# Drift detected: consumer repo's checked-in validation assets diverge
		# from what the current templates would render. The runner intentionally
		# does NOT push or open a PR — consumer repos render assets on demand
		# during their own validation flow. The drift is recorded for monitoring.
		result.diagnostics.append("validation_assets_drifted_no_push")
		result.outcome = "green" if pipeline_green else "red"
		return result

	def _resolve_default_branch(self, repository: str) -> tuple[str, str | None]:
		try:
			proc = self.executor.run(
				[
					"gh",
					"repo",
					"view",
					repository,
					"--json",
					"defaultBranchRef",
					"--jq",
					".defaultBranchRef.name",
				],
			)
			branch = (proc.stdout or "").strip()
			if branch:
				return branch, None
		except CommandFailure as exc:
			return "main", f"default_branch_lookup_failed_fallback_main: {exc.returncode}"
		return "main", "default_branch_lookup_empty_fallback_main"

	def _clone_repository(self, repository: str, repo_dir: Path) -> None:
		if repo_dir.exists():
			try:
				shutil.rmtree(repo_dir)
			except OSError as exc:
				print(f"WARNING: failed to remove existing repo dir {repo_dir}: {exc}")
			if repo_dir.exists():
				raise CommandFailure(
					command=("shutil", "rmtree", str(repo_dir)),
					cwd=None,
					returncode=1,
					stdout="",
					stderr=f"failed to remove existing directory: {repo_dir}",
				)
		self.executor.run(["gh", "repo", "clone", repository, str(repo_dir)])

	def _checkout_refresh_branch(self, repo_dir: Path, default_branch: str) -> None:
		# Drift monitoring always compares the rendered output against the
		# consumer repo's default branch. We deliberately ignore any existing
		# `origin/<branch_name>` that may linger from the previous PR-based
		# flow — otherwise drift against the default branch can be hidden by
		# a stale refresh branch. `branch_name` is now only a local label.
		self.executor.run(["git", "fetch", "origin", default_branch], cwd=repo_dir)
		self.executor.run(
			["git", "checkout", "-B", self.branch_name, f"origin/{default_branch}"],
			cwd=repo_dir,
		)

	def _run_refresh_pipeline(self, repo_dir: Path, manifest_path: Path) -> tuple[bool, list[str]]:
		diagnostics: list[str] = []
		pipeline_log_dir = repo_dir.parent / f"{repo_dir.name}__validation_logs"
		pipeline_env_overrides = {
			"GH_TOKEN": "",
			"GITHUB_TOKEN": "",
			"LOG_DIR": str(pipeline_log_dir),
		}

		render_command = [
			"python3",
			str(self.source_root / "scripts" / "render_validation_templates.py"),
			"--manifest",
			str(manifest_path),
			"--schema",
			str(self.source_root / "scripts" / "templates" / "slot_manifest.schema.json"),
			"--templates-root",
			str(self.source_root / "workflow-templates" / "validation-harness"),
			"--output-root",
			str(repo_dir / "validation"),
		]
		lint_command = [
			"python3",
			str(self.source_root / "scripts" / "validation_lint.py"),
			str(repo_dir / "validation"),
		]
		self_test_command = [
			"bash",
			str(self.source_root / "scripts" / "validate_driver.sh"),
		]

		for stage, command in (
			("render", render_command),
			("lint", lint_command),
			("self_test", self_test_command),
		):
			try:
				self.executor.run(command, cwd=repo_dir, env_overrides=pipeline_env_overrides)
			except CommandFailure as exc:
				diagnostics.append(_format_command_failure(stage, exc))
				return False, diagnostics

		return True, diagnostics

	def _repository_has_changes(self, repo_dir: Path) -> bool:
		proc = self.executor.run(
			["git", "status", "--porcelain", "--untracked-files=all"],
			cwd=repo_dir,
		)
		return bool((proc.stdout or "").strip())

	def _dispatch_discovery(
		self,
		*,
		result: RefreshResult,
		repo_dir: Path,
		repository: str,
		default_branch: str,
	) -> None:
		"""Codex-driven `.ai/validate.yml` discovery for one consumer repo.

		Q1:A — runs once per consumer per cycle when enabled.
		Q3:B — runs even when a committed manifest is present, opens a PR
			only on `type` mismatch (Q7:A).
		Q4:A — codex failure is recorded but never blocks the refresh job.
		Q5:A — 7-day dedup via `validation_discovery.v1` on the ai-memory
			branch keeps cost bounded.
		Q8:A — existing open PR on the same branch is reused, not churned.
		Q9:A — push denial is recorded and continues with the next consumer.
		"""

		ctx = self.discovery_ctx
		if ctx is None or not ctx.enabled:
			result.discovery_outcome = "disabled"
			print(f"VALIDATION_DISCOVERY_SKIPPED_DISABLED repository={repository}")
			return

		# Aggregate wall-clock budget gate (VALIDATION_DISCOVERY_BUDGET_SECS).
		# Only start this repo's codex discovery while enough budget remains to
		# cover its worst case (`codex_attempts * DISCOVERY_CODEX_CALL_TIMEOUT_SECS`),
		# so the dispatch phase cannot overrun the deadline. Past that point the
		# remaining repos skip codex but still flow through drift monitoring
		# below — discovery must never block the refresh job (runner Q4:A).
		if self._discovery_deadline is not None:
			remaining = self._discovery_deadline - time.monotonic()
			worst_case_single = ctx.codex_attempts * DISCOVERY_CODEX_CALL_TIMEOUT_SECS
			if remaining < worst_case_single:
				result.discovery_outcome = "skipped_budget"
				result.discovery_diagnostics.append(
					f"discovery_budget_exhausted: remaining={int(remaining)}s "
					f"worst_case_single={worst_case_single}s budget={ctx.discovery_budget_secs}s"
				)
				print(
					"VALIDATION_DISCOVERY_SKIPPED_BUDGET "
					f"repository={repository} remaining_secs={int(remaining)} "
					f"budget_secs={ctx.discovery_budget_secs}"
				)
				return

		# Q5:A dedup — skip when a successful discovery is recorded within window.
		if _dedup_skip(repository=repository, repo_root=self.source_root, dedup_days=ctx.dedup_days):
			result.discovery_outcome = "skipped_dedup"
			result.discovery_diagnostics.append("dedup_window_active")
			print(f"VALIDATION_DISCOVERY_SKIPPED_DEDUP repository={repository}")
			return

		consumer_head_sha = self._read_default_branch_head_sha(repo_dir)
		committed_manifest_text = self._read_committed_manifest_yaml(repo_dir)

		print(
			"VALIDATION_DISCOVERY_STARTED "
			f"repository={repository} consumer_head_sha={consumer_head_sha[:12] if consumer_head_sha else 'unknown'} "
			f"has_committed_manifest={'1' if committed_manifest_text else '0'}"
		)

		discovery_outcome_for_memory = "failed"
		discovered_type: str | None = None
		committed_type: str | None = None
		pr_url: str | None = None
		pr_branch: str | None = None
		codex_attempts_used: int = 0
		failure_reason: str | None = None

		if ctx.dry_run:
			# Dry-run mode short-circuits codex invocation but still exercises
			# the dedup + memory write so operators can validate the pipeline
			# end-to-end on `workflow_dispatch` without burning LLM cost.
			discovery_outcome_for_memory = "dry_run"
			result.discovery_outcome = "dry_run"
			print(f"VALIDATION_DISCOVERY_DRY_RUN repository={repository}")
		else:
			discovery_result = discovery_module.discover_manifest_via_codex(
				clone_dir=repo_dir,
				prompt_path=ctx.prompt_path,
				schema_path=ctx.schema_path,
				model=ctx.codex_model,
				reasoning_effort=ctx.codex_reasoning_effort,
				attempts=ctx.codex_attempts,
				executor=self.executor,
				per_call_timeout_secs=DISCOVERY_CODEX_CALL_TIMEOUT_SECS,
			)
			codex_attempts_used = discovery_result.attempts_used

			if discovery_result.outcome != "success" or discovery_result.manifest_yaml is None:
				failure_reason = discovery_result.failure_reason or "discovery_failed"
				discovery_outcome_for_memory = "failed"
				result.discovery_outcome = "failed"
				result.discovery_diagnostics.append(f"codex_failed: {failure_reason}")
				print(
					"VALIDATION_DISCOVERY_FAILED "
					f"repository={repository} attempts={codex_attempts_used} reason={failure_reason}"
				)
			else:
				discovered_type = (
					(discovery_result.parsed_manifest or {}).get("type")
					if isinstance(discovery_result.parsed_manifest, dict)
					else None
				)

				disagreement = discovery_module.classify_disagreement(
					committed_yaml_text=committed_manifest_text,
					discovered_yaml_text=discovery_result.manifest_yaml,
				)
				committed_type = disagreement.committed_type

				is_seed = committed_manifest_text is None

				if not is_seed and not disagreement.disagrees:
					# Q3:B + Q7:A — committed type matches discovered type. Record
					# `success_agree` for dedup but skip opening a PR.
					discovery_outcome_for_memory = "success_agree"
					result.discovery_outcome = "agree"
					print(
						"VALIDATION_DISCOVERY_AGREE "
						f"repository={repository} type={discovered_type or 'unknown'}"
					)
				else:
					# Open / reuse a PR with the discovered manifest.
					if not is_seed:
						print(
							"VALIDATION_DISCOVERY_DISAGREE "
							f"repository={repository} committed_type={committed_type or 'unknown'} "
							f"discovered_type={discovered_type or 'unknown'}"
						)
					pr_branch = discovery_module.build_pr_branch_name(
						prefix=ctx.pr_branch_prefix,
						consumer_head_sha=consumer_head_sha or "0" * 12,
						discovered_type=discovered_type or "unknown",
					)
					entry_script_text: str | None = None
					if is_seed and ctx.stub_entry_script_source.is_file():
						try:
							entry_script_text = ctx.stub_entry_script_source.read_text(encoding="utf-8")
						except OSError:
							entry_script_text = None

					pr_title = (
						f"chore(validation): seed .ai/validate.yml (type: {discovered_type or 'unknown'})"
						if is_seed
						else f"chore(validation): discovery proposes type change "
						f"({committed_type or 'unknown'} → {discovered_type or 'unknown'})"
					)
					rationale = discovery_module.build_pr_rationale_markdown(
						consumer_slug=repository,
						committed_type=committed_type,
						discovered_type=discovered_type or "unknown",
						consumer_head_sha=consumer_head_sha or "",
						codex_model=ctx.codex_model,
						reasoning_effort=ctx.codex_reasoning_effort,
						attempts_used=codex_attempts_used,
						is_seed=is_seed,
					)
					pr_result = discovery_module.open_or_update_discovery_pr(
						consumer_slug=repository,
						consumer_clone_dir=repo_dir,
						base_branch=default_branch,
						pr_branch=pr_branch,
						manifest_yaml=discovery_result.manifest_yaml,
						entry_script_text=entry_script_text,
						entry_script_relative_path="scripts/run_validation_repo_checks.sh",
						pr_title=pr_title,
						rationale_md=rationale,
						label=ctx.pr_label,
						executor=self.executor,
					)

					if pr_result.failure_reason and pr_result.failure_reason.startswith("push_denied"):
						discovery_outcome_for_memory = "push_denied"
						failure_reason = pr_result.failure_reason
						result.discovery_outcome = "push_denied"
						result.discovery_diagnostics.append(pr_result.failure_reason)
						print(
							f"VALIDATION_DISCOVERY_FAILED repository={repository} reason={pr_result.failure_reason}"
						)
					elif pr_result.failure_reason:
						discovery_outcome_for_memory = "failed"
						failure_reason = pr_result.failure_reason
						result.discovery_outcome = "failed"
						result.discovery_diagnostics.append(pr_result.failure_reason)
						print(
							f"VALIDATION_DISCOVERY_FAILED repository={repository} reason={pr_result.failure_reason}"
						)
					else:
						pr_url = pr_result.pr_url
						result.discovery_pr_url = pr_url
						result.discovery_pr_branch = pr_branch
						if pr_result.pr_was_reused:
							discovery_outcome_for_memory = (
								"success_seeded" if is_seed else "success_disagree"
							)
							result.discovery_outcome = "pr_reused"
							print(
								"VALIDATION_DISCOVERY_PR_REUSED "
								f"repository={repository} pr_url={pr_url}"
							)
						else:
							discovery_outcome_for_memory = (
								"success_seeded" if is_seed else "success_disagree"
							)
							result.discovery_outcome = "pr_opened"
							print(
								"VALIDATION_DISCOVERY_PR_OPENED "
								f"repository={repository} pr_url={pr_url} kind={'seed' if is_seed else 'disagree'}"
							)

		# Record outcome on the ai-memory branch (fail-open). Failed and dry-run
		# entries are kept for observability; the dedup gate counts only
		# `success_*` entries.
		_append_discovery_memory(
			repository=repository,
			repo_root=self.source_root,
			outcome=discovery_outcome_for_memory,
			consumer_head_sha=consumer_head_sha,
			consumer_default_branch=default_branch,
			discovered_type=discovered_type,
			committed_type=committed_type,
			pr_url=pr_url,
			pr_branch=pr_branch,
			codex_attempts_used=codex_attempts_used or None,
			failure_reason=failure_reason,
		)

	def _read_default_branch_head_sha(self, repo_dir: Path) -> str | None:
		try:
			proc = self.executor.run(
				["git", "rev-parse", "HEAD"],
				cwd=repo_dir,
				check=False,
			)
		except CommandFailure:
			return None
		text = (proc.stdout or "").strip()
		return text or None

	def _read_committed_manifest_yaml(self, repo_dir: Path) -> str | None:
		manifest_path = repo_dir / ".ai" / "validate.yml"
		if not manifest_path.is_file():
			return None
		try:
			return manifest_path.read_text(encoding="utf-8")
		except OSError:
			return None


def load_target_repositories(repos_file: Path) -> list[str]:
	if not repos_file.is_file():
		raise ValueError(f"repository registry not found: {repos_file}")
	try:
		payload = json.loads(repos_file.read_text(encoding="utf-8"))
	except json.JSONDecodeError as exc:
		raise ValueError(f"repository registry is not valid JSON: {exc}") from exc
	if not isinstance(payload, list):
		raise ValueError("repository registry must be a JSON array of \"owner/repo\" strings")

	repositories: list[str] = []
	seen: set[str] = set()
	for index, item in enumerate(payload):
		if not isinstance(item, str):
			raise ValueError(f"repository entry at index {index} is not a string")
		repo = item.strip()
		if not REPO_NAME_RE.match(repo):
			raise ValueError(f"repository entry at index {index} is invalid: {repo!r}")
		if repo in seen:
			continue
		seen.add(repo)
		repositories.append(repo)
	return repositories


def summarize_results(results: list[RefreshResult]) -> dict[str, Any]:
	totals = {
		"processed": len(results),
		"green": 0,
		"red": 0,
		"skipped": 0,
		"error": 0,
	}
	for result in results:
		if result.outcome in totals:
			totals[result.outcome] += 1
		else:
			totals["error"] += 1

	return {
		"generated_at": datetime.now(timezone.utc).isoformat(),
		"totals": totals,
		"results": [asdict(item) for item in results],
	}


def _compact_log_value(value: str | None) -> str:
	if value is None:
		return ""
	return " ".join(str(value).split())


def _emit_repo_result(result: RefreshResult) -> None:
	diagnostics_count = len(result.diagnostics)
	discovery = result.discovery_outcome or "none"
	changed = "1" if result.changed else "0"
	drifted = (
		"1"
		if any(item == "validation_assets_drifted_no_push" for item in result.diagnostics)
		else "0"
	)
	parts = [
		"VALIDATION_REPO_RESULT",
		f"repository={result.repository}",
		f"outcome={result.outcome}",
		f"changed={changed}",
		f"drifted={drifted}",
		f"discovery_outcome={discovery}",
		f"diagnostics={diagnostics_count}",
	]
	if result.discovery_pr_url:
		parts.append(f"discovery_pr_url={json.dumps(result.discovery_pr_url)}")
	if result.diagnostics:
		parts.append(f"detail={json.dumps(_compact_log_value(result.diagnostics[-1]))}")
	print(" ".join(parts))


def _count_budget_exhausted(results: list[RefreshResult]) -> int:
	return sum(1 for result in results if result.discovery_outcome == "skipped_budget")


def _emit_summary_line(summary: dict[str, Any], results: list[RefreshResult]) -> None:
	totals = summary.get("totals")
	if not isinstance(totals, dict):
		return
	processed = totals.get("processed", 0)
	green = totals.get("green", 0)
	red = totals.get("red", 0)
	skipped = totals.get("skipped", 0)
	error = totals.get("error", 0)
	budget_exhausted = _count_budget_exhausted(results)
	print(
		"VALIDATION_SUMMARY "
		f"processed={processed} "
		f"green={green} "
		f"red={red} "
		f"skipped={skipped} "
		f"error={error} "
		f"budget_exhausted={budget_exhausted}"
	)


def write_summary(summary: dict[str, Any], summary_path: Path | None) -> None:
	if summary_path is None:
		return
	summary_path.parent.mkdir(parents=True, exist_ok=True)
	summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--repos-file",
		type=Path,
		default=Path(".github/ai/consumer_repos.json"),
		help="JSON file containing consumer repositories as owner/repo strings",
	)
	parser.add_argument(
		"--workspace-root",
		type=Path,
		default=None,
		help="Optional workspace root for cloned repositories (default: temporary directory)",
	)
	parser.add_argument(
		"--summary-json",
		type=Path,
		default=None,
		help="Optional output file for machine-readable summary",
	)
	parser.add_argument(
		"--branch-name",
		default="ai/validation-refresh",
		help="Local branch name used inside the temp clone during render/lint/self-test",
	)
	parser.add_argument(
		"--commit-message",
		default="chore(validation): refresh validation assets",
		help="Deprecated, no-op: kept for backward compatibility with prior runner CLI",
	)
	parser.add_argument(
		"--pr-title",
		default="chore(validation): refresh validation assets",
		help="Deprecated, no-op: kept for backward compatibility with prior runner CLI",
	)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	source_root = Path(__file__).resolve().parent.parent

	try:
		repositories = load_target_repositories(args.repos_file)
	except (OSError, ValueError) as exc:
		print(f"ERROR: {exc}")
		return 1

	if not repositories:
		summary = summarize_results([])
		write_summary(summary, args.summary_json)
		_emit_summary_line(summary, [])
		print(json.dumps(summary, sort_keys=True))
		return 0

	workspace_path = args.workspace_root
	temporary_root: tempfile.TemporaryDirectory[str] | None = None
	if workspace_path is None:
		temporary_root = tempfile.TemporaryDirectory(prefix="validation-refresh-")
		workspace_path = Path(temporary_root.name)
	workspace_path.mkdir(parents=True, exist_ok=True)

	results: list[RefreshResult] = []
	try:
		runner = ValidationRefreshRunner(
			source_root=source_root,
			branch_name=args.branch_name,
			commit_message=args.commit_message,
			pr_title=args.pr_title,
		)
		results = runner.run_repositories(repositories, workspace_path)
		for result in results:
			_emit_repo_result(result)
		summary = summarize_results(results)
		write_summary(summary, args.summary_json)
		_emit_summary_line(summary, results)
		print(json.dumps(summary, sort_keys=True))
	finally:
		if temporary_root is not None:
			temporary_root.cleanup()

	# Only genuine refresh-mechanism failures (`error`: clone/checkout/manifest
	# bootstrap failures, invalid repo names, unexpected exceptions) fail the
	# runner and therefore gate the release smoke (`orphan-workflows-test` in
	# test-and-mark-stable.yml watches this run's conclusion). Consumer-repo
	# outcomes — `red` (render/lint/self_test failed), `green`, `skipped` —
	# are recorded for monitoring but intentionally non-blocking, so one
	# unhealthy downstream consumer never blocks a stable release of this
	# library.
	return 1 if any(result.outcome == "error" for result in results) else 0


def _build_default_discovery_ctx(source_root: Path) -> "discovery_module.DiscoveryRunContext":
	"""Resolve discovery configuration from env vars with documented defaults.

	Env vars (all §4 defaults supplied):
	- VALIDATION_DISCOVERY_ENABLED            (default: true)
	- VALIDATION_DISCOVERY_DEDUP_DAYS         (default: 7)
	- VALIDATION_DISCOVERY_MAX_ATTEMPTS       (default: 3)
	- VALIDATION_DISCOVERY_MODEL              (default: openai/gpt-5.6-sol)
	- VALIDATION_DISCOVERY_REASONING_EFFORT   (default: xhigh)
	- VALIDATION_DISCOVERY_PR_BRANCH_PREFIX   (default: automation/validate-discovery)
	- VALIDATION_DISCOVERY_PR_LABEL           (default: automation:validate-bootstrap)
	- VALIDATION_DISCOVERY_DRY_RUN            (default: false)
	- VALIDATION_DISCOVERY_BUDGET_SECS        (default: 2100)
	"""

	return discovery_module.DiscoveryRunContext(
		source_root=source_root,
		prompt_path=source_root / "prompts" / "mode-validate-discover.txt",
		schema_path=source_root / "scripts" / "templates" / "slot_manifest.schema.json",
		stub_entry_script_source=source_root
		/ "examples"
		/ "validation-fixtures"
		/ "run_validation_repo_checks.sh",
		codex_model=_env_str("VALIDATION_DISCOVERY_MODEL", "openai/gpt-5.6-sol"),
		codex_reasoning_effort=_env_str("VALIDATION_DISCOVERY_REASONING_EFFORT", "xhigh"),
		codex_attempts=_env_int("VALIDATION_DISCOVERY_MAX_ATTEMPTS", 3),
		pr_branch_prefix=_env_str(
			"VALIDATION_DISCOVERY_PR_BRANCH_PREFIX", "automation/validate-discovery"
		),
		pr_label=_env_str("VALIDATION_DISCOVERY_PR_LABEL", "automation:validate-bootstrap") or None,
		dedup_days=_env_int("VALIDATION_DISCOVERY_DEDUP_DAYS", 7),
		discovery_budget_secs=_env_int("VALIDATION_DISCOVERY_BUDGET_SECS", 2100, minimum=-sys.maxsize),
		enabled=_env_bool("VALIDATION_DISCOVERY_ENABLED", True),
		dry_run=_env_bool("VALIDATION_DISCOVERY_DRY_RUN", False),
	)


def _dedup_skip(*, repository: str, repo_root: Path, dedup_days: int) -> bool:
	"""Return True when a recent successful discovery makes another run redundant.

	Reads `validation_discovery.v1` from ai-memory via the existing
	`memory_helpers.sh` shell wrapper (fail-open). Counts only
	`success_seeded` / `success_agree` / `success_disagree` outcomes — failed
	discoveries do NOT block re-attempts on the next cycle.
	"""

	if dedup_days <= 0:
		return False
	helper_path = repo_root / "scripts" / "memory_helpers.sh"
	if not helper_path.is_file():
		return False
	try:
		proc = subprocess.run(
			[
				"bash",
				"-c",
				'. "$1" && memory_validation_discovery_get --repo "$2" --enabled',
				"bash",
				helper_path.as_posix(),
				repository,
			],
			cwd=str(repo_root),
			text=True,
			capture_output=True,
			timeout=120,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		return False
	if proc.returncode != 0:
		return False
	stdout = (proc.stdout or "").strip()
	if not stdout:
		return False
	try:
		payload = json.loads(stdout)
	except json.JSONDecodeError:
		return False
	if not isinstance(payload, dict):
		return False
	discovery = payload.get("validation_discovery")
	if not isinstance(discovery, dict):
		return False
	entries = discovery.get("entries")
	if not isinstance(entries, list):
		return False

	threshold = datetime.now(timezone.utc).timestamp() - (dedup_days * 86400)
	for entry in entries:
		if not isinstance(entry, dict):
			continue
		outcome = entry.get("outcome")
		if outcome not in ("success_seeded", "success_agree", "success_disagree"):
			continue
		recorded_at = entry.get("recorded_at")
		if not isinstance(recorded_at, str):
			continue
		try:
			ts = datetime.fromisoformat(recorded_at.replace("Z", "+00:00")).timestamp()
		except ValueError:
			continue
		if ts >= threshold:
			return True
	return False


def _append_discovery_memory(
	*,
	repository: str,
	repo_root: Path,
	outcome: str,
	consumer_head_sha: str | None,
	consumer_default_branch: str | None,
	discovered_type: str | None,
	committed_type: str | None,
	pr_url: str | None,
	pr_branch: str | None,
	codex_attempts_used: int | None,
	failure_reason: str | None,
) -> None:
	"""Persist a single discovery outcome to ai-memory (fail-open)."""

	helper_path = repo_root / "scripts" / "memory_helpers.sh"
	if not helper_path.is_file():
		return

	entry = {
		"outcome": outcome,
		"recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
		"consumer_head_sha": consumer_head_sha,
		"consumer_default_branch": consumer_default_branch,
		"discovered_type": discovered_type,
		"committed_type": committed_type,
		"pr_url": pr_url,
		"pr_branch": pr_branch,
		"codex_attempts_used": codex_attempts_used,
		"failure_reason": failure_reason,
	}

	with tempfile.NamedTemporaryFile(
		mode="w", encoding="utf-8", suffix=".json", delete=False
	) as handle:
		json.dump(entry, handle)
		entry_file = handle.name

	try:
		subprocess.run(
			[
				"bash",
				"-c",
				'. "$1" && memory_validation_discovery_append --repo "$2" --entry-file "$3" --enabled',
				"bash",
				helper_path.as_posix(),
				repository,
				entry_file,
			],
			cwd=str(repo_root),
			text=True,
			capture_output=True,
			timeout=180,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		# Fail-open — memory write failure does not block the refresh job.
		pass
	finally:
		try:
			os.unlink(entry_file)
		except OSError:
			pass


def _format_command_failure(stage: str, failure: CommandFailure) -> str:
	stderr = (failure.stderr or "").strip()
	stdout = (failure.stdout or "").strip()
	if stderr:
		detail = " | ".join(line.strip() for line in stderr.splitlines()[:3] if line.strip())
	elif stdout:
		detail = " | ".join(line.strip() for line in stdout.splitlines()[:3] if line.strip())
	else:
		detail = "no_output"
	return f"{stage}_failed(exit={failure.returncode}): {detail}"


if __name__ == "__main__":
	raise SystemExit(main())
