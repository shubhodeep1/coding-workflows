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

# SHA segment of a discovery PR branch (`<prefix>/<sha>/<type>`). Hex only, so
# a branch name can never reach `git merge-base` as an option.
_DISCOVERY_BRANCH_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Per-`codex exec` wall-clock cap (seconds) for the discovery dispatch. This
# is the unit the aggregate discovery budget (VALIDATION_DISCOVERY_BUDGET_SECS)
# reasons about: a single repo's discovery can consume at most
# `codex_attempts * DISCOVERY_CODEX_CALL_TIMEOUT_SECS`, so the runner only
# starts a repo's discovery while that much budget remains. Matches the
# CommandExecutor default so the explicit cap and the budget math agree.
DISCOVERY_CODEX_CALL_TIMEOUT_SECS = 300

# `gh repo clone` attempts per consumer repo, and the waits between them.
# A clone failure is an `error` outcome, which fails the runner and with it
# the release smoke (`orphan-workflows-test`). One transient GitHub answer
# must not do that: release run 38253656098 (2026-10-12) failed because a
# single clone got "GraphQL: Something went wrong while executing your
# query". A failure that persists through every attempt still reports
# `error`.
CLONE_ATTEMPTS = 3
CLONE_RETRY_DELAYS_SECS = (5, 15)

# Wall-clock cap (seconds) for the sandboxed consumer self-test of one repo.
# `CommandExecutor.run` defaults to 300 s, which a large monorepo's
# `docker compose build` + tests exceeds (bitsafe.io exits 124). Override with
# VALIDATION_REFRESH_SELF_TEST_TIMEOUT_SECONDS; a missing, non-numeric or
# non-positive value falls back to the default and a larger value is clamped
# to the maximum. The maximum is half the job's 60-minute `timeout-minutes`,
# so the runner's own timeout fires (and its exit-124 diagnostic and run
# summary are written) before GitHub kills the job.
SELF_TEST_TIMEOUT_ENV = "VALIDATION_REFRESH_SELF_TEST_TIMEOUT_SECONDS"
SELF_TEST_TIMEOUT_DEFAULT_SECS = 900
SELF_TEST_TIMEOUT_MAX_SECS = 1800

# Bounds for the failure text a stage puts into its diagnostic. The text is
# consumer-controlled and ends up in the Actions log, so it is size-limited
# and sanitised by `_sanitized_failure_lines`.
FAILURE_DETAIL_HEAD_LINES = 3
FAILURE_DETAIL_TAIL_LINES = 20
FAILURE_DETAIL_LINE_MAX_CHARS = 500
FAILURE_DETAIL_LOG_TAIL_MAX_CHARS = 1800
FAILURE_DETAIL_COMPOSE_TAIL_MAX_CHARS = 1200
FAILURE_DETAIL_MAX_CHARS = 4096
FAILURE_DETAIL_FILE_READ_BYTES = 65536

# Control-plane credentials the refresh process may hold (the workflow step
# sets GH_TOKEN from GH_PAT and OPENROUTER_API_KEY). Every pipeline
# subprocess that works on the untrusted consumer clone (render, lint,
# sandbox provision, checked-run, cleanup) gets these blanked, so they stay
# in this process. Discovery's isolated Codex launch is not a pipeline
# subprocess and keeps OPENROUTER_API_KEY for its host-side broker (#7351).
REFRESH_PIPELINE_CREDENTIAL_ENV_NAMES = (
	"GH_TOKEN",
	"GITHUB_TOKEN",
	"GH_PAT",
	"OPENROUTER_API_KEY",
	"TG_BOT_SECRET",
	"CHECK_TRIAGE_ISSUES_TOKEN",
	"CLAUDE_CODE_OAUTH_TOKEN",
	"ANTHROPIC_API_KEY",
	# validation-refresh.yml grants id-token: write; keep the OIDC request
	# credentials out of the pipeline subprocesses too.
	"ACTIONS_ID_TOKEN_REQUEST_TOKEN",
	"ACTIONS_ID_TOKEN_REQUEST_URL",
)


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


def _self_test_timeout_secs() -> int:
	configured = _env_int(SELF_TEST_TIMEOUT_ENV, SELF_TEST_TIMEOUT_DEFAULT_SECS)
	return min(configured, SELF_TEST_TIMEOUT_MAX_SECS)


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
		# The harness sandbox user and its rootless dockerd are provisioned
		# once per runner; later repositories reuse them.
		self._harness_sandbox_provisioned = False

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
		if not self._validation_assets_tracked(repo_dir):
			# `changed` only means "working tree not clean after render". With
			# nothing tracked under validation/ the dirty files (rendered output,
			# or the bootstrapped manifest stubs) are simply untracked, so this is not drift against committed assets. The
			# line above (and `drifted=1`) is kept for log compatibility;
			# this last diagnostic is the one `detail=` shows.
			result.diagnostics.append(
				"validation_assets_not_committed: no files under validation/ are tracked on the "
				"default branch, so the dirty tree is uncommitted rendered or bootstrapped files, "
				"not drift against committed assets"
			)
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
		"""Clone with up to CLONE_ATTEMPTS tries; re-raise the last failure.

		Logs VALIDATION_REFRESH_CLONE_RETRY repository=<r> attempt=<n> max=<m>
		returncode=<rc> delay_secs=<s> before each retry.
		"""
		for attempt in range(1, CLONE_ATTEMPTS + 1):
			try:
				self._clone_repository_once(repository, repo_dir)
				return
			except CommandFailure as exc:
				if attempt >= CLONE_ATTEMPTS:
					raise
				delay = CLONE_RETRY_DELAYS_SECS[min(attempt - 1, len(CLONE_RETRY_DELAYS_SECS) - 1)]
				print(
					f"VALIDATION_REFRESH_CLONE_RETRY repository={repository} attempt={attempt} "
					f"max={CLONE_ATTEMPTS} returncode={exc.returncode} delay_secs={delay}"
				)
				time.sleep(delay)

	def _clone_repository_once(self, repository: str, repo_dir: Path) -> None:
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
		# Blank every control-plane credential for the pipeline subprocesses;
		# none of them needs one, and they run against the untrusted clone.
		pipeline_env_overrides = {name: "" for name in REFRESH_PIPELINE_CREDENTIAL_ENV_NAMES}
		pipeline_env_overrides["LOG_DIR"] = str(pipeline_log_dir)

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
		# Consumer validation tests are untrusted and this process holds
		# GH_TOKEN / OPENROUTER_API_KEY, which a same-user test could read
		# from /proc. The self-test therefore runs only through
		# scripts/validation_harness_sandbox.sh (separate user, rootless
		# Docker, allowlisted env, selfcheck against this pid). Any sandbox
		# failure is a self_test failure; there is no host fallback. The
		# runner-UID helper calls (provision, checked-run, cleanup) also get
		# the blanked pipeline_env_overrides, so no refresh credential leaves
		# this process (#7351).
		sandbox_script = str(self.source_root / "scripts" / "validation_harness_sandbox.sh")
		self_test_command = [
			"bash",
			sandbox_script,
			"checked-run",
			str(os.getpid()),
			str(self.source_root / "scripts" / "validate_driver.sh"),
		]
		self_test_timeout_secs = _self_test_timeout_secs()
		self_test_env_overrides = {
			**pipeline_env_overrides,
			# Inside the sandbox copy the driver writes its default
			# validation/logs; the helper copies them back to the old
			# out-of-clone location so drift detection is unaffected.
			"LOG_DIR": "validation/logs",
			"VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST": str(pipeline_log_dir),
		}

		# Same manifest shell-safety gate as validate_process.sh's
		# run_template_validation_harness_renderer: this path renders the
		# consumer manifest and runs the tests without going through it
		# (finding validation-manifest-shell-injection-fallback).
		safety_violations = manifest_shell_safety_violations(manifest_path)
		if safety_violations:
			diagnostics.append("manifest_shell_safety_failed: " + "; ".join(safety_violations[:3]))
			return False, diagnostics

		for stage, command in (
			("render", render_command),
			("lint", lint_command),
		):
			try:
				self.executor.run(command, cwd=repo_dir, env_overrides=pipeline_env_overrides)
			except CommandFailure as exc:
				diagnostics.append(_format_command_failure(stage, exc))
				return False, diagnostics

		try:
			if not self._harness_sandbox_provisioned:
				self.executor.run(
					["bash", sandbox_script, "provision"],
					cwd=repo_dir,
					env_overrides=pipeline_env_overrides,
					timeout=600,
				)
				self._harness_sandbox_provisioned = True
			self.executor.run(
				self_test_command,
				cwd=repo_dir,
				env_overrides=self_test_env_overrides,
				timeout=self_test_timeout_secs,
			)
		except CommandFailure as exc:
			diagnostics.append(
				_format_command_failure(
					"self_test",
					exc,
					compose_log_path=pipeline_log_dir / "compose.log",
					timeout_secs=self_test_timeout_secs,
				)
			)
			# A timeout kills only the helper; stop the sandbox user's harness.
			try:
				self.executor.run(
					["bash", sandbox_script, "cleanup"],
					cwd=repo_dir,
					check=False,
					env_overrides=pipeline_env_overrides,
				)
			except CommandFailure:
				pass
			return False, diagnostics

		return True, diagnostics

	def _validation_assets_tracked(self, repo_dir: Path) -> bool:
		"""True unless git proves nothing under validation/ is tracked.

		A failed lookup counts as tracked, so the not-committed diagnostic is
		only emitted on positive evidence.
		"""
		try:
			proc = self.executor.run(
				["git", "ls-files", "--", "validation"],
				cwd=repo_dir,
				check=False,
				# A local index read; fail fast instead of the 300 s default.
				timeout=30,
			)
		except CommandFailure:
			# Timeout or missing binary raise even with check=False.
			return True
		if proc.returncode != 0:
			return True
		return bool((proc.stdout or "").strip())

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

		committed_manifest_text = self._read_committed_manifest_yaml(repo_dir)

		# Close obsolete discovery PRs before the dedup and budget gates, so a
		# seed PR left open after a manifest landed is closed on the next cycle
		# instead of after the dedup window (btc_sweeper#196 conflicted with the
		# seed merged by #235 and blocked review/autofix on a host-only path).
		remaining_discovery_prs = self._sweep_superseded_discovery_prs(
			result=result,
			repository=repository,
			default_branch=default_branch,
			committed_manifest_text=committed_manifest_text,
		)

		# Dedup runs before the budget gate: a repo with a recent successful
		# discovery is `skipped_dedup` whether or not budget remains, so late repos
		# no longer report `skipped_budget` for a skip they would have had anyway.
		# Q5:A dedup — skip when a successful discovery is recorded within window.
		if _dedup_skip(repository=repository, repo_root=self.source_root, dedup_days=ctx.dedup_days):
			result.discovery_outcome = "skipped_dedup"
			result.discovery_diagnostics.append("dedup_window_active")
			print(f"VALIDATION_DISCOVERY_SKIPPED_DEDUP repository={repository}")
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

		consumer_head_sha = self._read_default_branch_head_sha(repo_dir)

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
						f"{discovery_module.SEED_PR_TITLE_PREFIX} (type: {discovered_type or 'unknown'})"
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
						# The opened or reused PR replaces every older discovery
						# PR on this consumer. Reuses the earlier listing (§15).
						# Only PRs opened for this run's consumer HEAD or an
						# ancestor of it count as older: a concurrent run with
						# another branch_name may already have opened a PR for a
						# newer HEAD, which this run must not close.
						older_discovery_prs = (
							self._discovery_prs_at_or_before_head(
								result=result,
								repository=repository,
								repo_dir=repo_dir,
								consumer_head_sha=consumer_head_sha,
								open_prs=remaining_discovery_prs,
								replacement_branch=pr_branch,
							)
							if remaining_discovery_prs
							else []
						)
						if older_discovery_prs:
							self._sweep_superseded_discovery_prs(
								result=result,
								repository=repository,
								default_branch=default_branch,
								committed_manifest_text=committed_manifest_text,
								open_prs=older_discovery_prs,
								replacement_branch=pr_branch,
								replacement_url=pr_url,
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

	def _sweep_superseded_discovery_prs(
		self,
		*,
		result: RefreshResult,
		repository: str,
		default_branch: str,
		committed_manifest_text: str | None,
		open_prs: "list[discovery_module.OpenDiscoveryPr] | None" = None,
		replacement_branch: str | None = None,
		replacement_url: str | None = None,
	) -> "list[discovery_module.OpenDiscoveryPr] | None":
		"""Close obsolete discovery PRs on one consumer; never blocks discovery.

		With `open_prs=None` it lists them first (one `gh pr list`); a caller
		that already holds the list passes it in. Returns the discovery PRs
		still open afterwards, or None when the sweep is off or the listing
		failed (the caller then skips its second pass).
		"""

		ctx = self.discovery_ctx
		if ctx is None or not ctx.close_superseded:
			return None
		try:
			if open_prs is None:
				open_prs, list_error = discovery_module.list_open_discovery_prs(
					consumer_slug=repository,
					base_branch=default_branch,
					pr_branch_prefix=ctx.pr_branch_prefix,
					executor=self.executor,
				)
				if list_error:
					result.discovery_diagnostics.append(f"superseded_sweep_list_failed: {list_error}")
					print(
						"VALIDATION_DISCOVERY_SUPERSEDED "
						f"repository={repository} outcome=list_failed "
						f"detail={json.dumps(_compact_log_value(list_error))}"
					)
					return None
			outcomes = discovery_module.close_superseded_discovery_prs(
				consumer_slug=repository,
				base_branch=default_branch,
				pr_branch_prefix=ctx.pr_branch_prefix,
				open_prs=open_prs,
				committed_type=discovery_module.manifest_type_from_yaml(committed_manifest_text),
				manifest_committed=committed_manifest_text is not None,
				replacement_branch=replacement_branch,
				replacement_url=replacement_url,
				dry_run=ctx.dry_run,
				executor=self.executor,
			)
		except Exception as exc:  # fail open: the sweep must never block discovery (Q4:A)
			result.discovery_diagnostics.append(f"superseded_sweep_error: {exc!r}")
			print(
				"VALIDATION_DISCOVERY_SUPERSEDED "
				f"repository={repository} outcome=error "
				f"detail={json.dumps(_compact_log_value(repr(exc)))}"
			)
			return None

		closed_numbers: set[int] = set()
		for outcome in outcomes:
			parts = [
				"VALIDATION_DISCOVERY_SUPERSEDED",
				f"repository={repository}",
				f"pr={outcome.number}",
				f"branch={outcome.head_branch}",
				f"reason={outcome.reason}",
				f"outcome={outcome.outcome}",
			]
			if replacement_url:
				parts.append(f"replacement={json.dumps(replacement_url)}")
			if outcome.detail:
				parts.append(f"detail={json.dumps(_compact_log_value(outcome.detail))}")
			print(" ".join(parts))
			result.discovery_diagnostics.append(
				f"superseded_pr: #{outcome.number} reason={outcome.reason} outcome={outcome.outcome}"
			)
			# A PR kept for human commits stays open but is dropped from the
			# returned list, so the post-open pass does not re-check it.
			if outcome.outcome in ("closed", "kept_human_commits"):
				closed_numbers.add(outcome.number)
		return [pr for pr in open_prs if pr.number not in closed_numbers]

	def _discovery_prs_at_or_before_head(
		self,
		*,
		result: RefreshResult,
		repository: str,
		repo_dir: Path,
		consumer_head_sha: str | None,
		open_prs: "list[discovery_module.OpenDiscoveryPr]",
		replacement_branch: str | None,
	) -> "list[discovery_module.OpenDiscoveryPr]":
		"""Return the discovery PRs whose branch SHA is the consumer HEAD or an ancestor of it.

		The branch is `<prefix>/<sha>/<type>`. A PR whose SHA is newer, not in
		the clone, or not a hex SHA is kept open and logged as `kept_not_older`.
		The replacement branch itself is dropped. Cost: one local
		`git merge-base --is-ancestor` per PR, no GitHub API call.
		"""

		ctx = self.discovery_ctx
		prefix = (
			discovery_module._slugify_for_branch_prefix(ctx.pr_branch_prefix) + "/"
			if ctx is not None
			else ""
		)
		older: "list[discovery_module.OpenDiscoveryPr]" = []
		for pr in open_prs:
			if pr.head_branch == replacement_branch:
				continue
			parts = (
				pr.head_branch[len(prefix):].split("/")
				if prefix and pr.head_branch.startswith(prefix)
				else []
			)
			pr_sha = parts[0] if len(parts) == 2 else ""
			is_older = False
			if consumer_head_sha and _DISCOVERY_BRANCH_SHA_RE.match(pr_sha):
				try:
					proc = self.executor.run(
						["git", "merge-base", "--is-ancestor", pr_sha, consumer_head_sha],
						cwd=repo_dir,
						check=False,
						timeout=30,
					)
					is_older = proc.returncode == 0
				except CommandFailure:
					is_older = False
			if is_older:
				older.append(pr)
				continue
			print(
				"VALIDATION_DISCOVERY_SUPERSEDED "
				f"repository={repository} pr={pr.number} branch={pr.head_branch} "
				"reason=superseded_by_newer_pr outcome=kept_not_older"
			)
			result.discovery_diagnostics.append(
				f"superseded_pr: #{pr.number} reason=superseded_by_newer_pr outcome=kept_not_older"
			)
		return older

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
	- VALIDATION_DISCOVERY_MODEL              (default: openai/gpt-6-sol)
	- VALIDATION_DISCOVERY_REASONING_EFFORT   (default: high)
	- VALIDATION_DISCOVERY_PR_BRANCH_PREFIX   (default: automation/validate-discovery)
	- VALIDATION_DISCOVERY_PR_LABEL           (default: automation:validate-bootstrap)
	- VALIDATION_DISCOVERY_DRY_RUN            (default: false)
	- VALIDATION_DISCOVERY_BUDGET_SECS        (default: 2100)
	- VALIDATION_DISCOVERY_CLOSE_SUPERSEDED   (default: true)
	"""

	return discovery_module.DiscoveryRunContext(
		source_root=source_root,
		prompt_path=source_root / "prompts" / "mode-validate-discover.txt",
		schema_path=source_root / "scripts" / "templates" / "slot_manifest.schema.json",
		stub_entry_script_source=source_root
		/ "examples"
		/ "validation-fixtures"
		/ "run_validation_repo_checks.sh",
		codex_model=_env_str("VALIDATION_DISCOVERY_MODEL", "openai/gpt-6-sol"),
		codex_reasoning_effort=_env_str("VALIDATION_DISCOVERY_REASONING_EFFORT", "high"),
		codex_attempts=_env_int("VALIDATION_DISCOVERY_MAX_ATTEMPTS", 3),
		pr_branch_prefix=_env_str(
			"VALIDATION_DISCOVERY_PR_BRANCH_PREFIX", "automation/validate-discovery"
		),
		pr_label=_env_str("VALIDATION_DISCOVERY_PR_LABEL", "automation:validate-bootstrap") or None,
		dedup_days=_env_int("VALIDATION_DISCOVERY_DEDUP_DAYS", 7),
		discovery_budget_secs=_env_int("VALIDATION_DISCOVERY_BUDGET_SECS", 2100, minimum=-sys.maxsize),
		enabled=_env_bool("VALIDATION_DISCOVERY_ENABLED", True),
		dry_run=_env_bool("VALIDATION_DISCOVERY_DRY_RUN", False),
		close_superseded=_env_bool("VALIDATION_DISCOVERY_CLOSE_SUPERSEDED", True),
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


MANIFEST_SHELL_SAFETY_MAX_BYTES = 2 * 1024 * 1024
MANIFEST_SHELL_SAFETY_MAX_DEPTH = 32
# Keep in step with the gate in scripts/validate_process.sh
# (run_template_validation_harness_renderer).
MANIFEST_SHELL_SAFETY_EXEMPT_KEYS = frozenset(
	{"custom_tests", "skip_tests", "env_overrides", "health_check", "services", "port"}
)


def _manifest_shell_safety_pointer(parts: list[Any]) -> str:
	if not parts:
		return "$"
	text = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)
	text = "".join(ch if ch.isprintable() and ch not in "\"'$`\\" else "?" for ch in text)
	return text if len(text) <= 120 else text[:117] + "..."


def _manifest_shell_unsafe_classes(text: str) -> list[str]:
	found = []
	if "$" in text:
		found.append("dollar")
	if "`" in text:
		found.append("backtick")
	if '"' in text or "'" in text:
		found.append("quote")
	if "\\" in text:
		found.append("backslash")
	if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
		found.append("control")
	return found


def manifest_shell_safety_violations(manifest_path: Path) -> list[str]:
	"""Return sanitized violations for shell-unsafe `.ai/validate.yml` values.

	The renderer writes manifest values unescaped into validation test scripts
	that validate_driver.sh runs with bash, so values outside the exempt keys
	must not hold quotes, `$`, backticks, backslashes, control characters or
	non-scalar YAML types. Messages name a sanitized pointer, never the value.
	Fails closed when the manifest cannot be read or PyYAML is unavailable;
	parse errors and non-mapping roots are left to the renderer.
	"""
	try:
		import yaml
	except ImportError:
		return ["$: PyYAML unavailable; manifest shell-safety check not run"]
	try:
		with open(manifest_path, "rb") as handle:
			raw = handle.read(MANIFEST_SHELL_SAFETY_MAX_BYTES + 1)
	except OSError as exc:
		return [f"$: manifest unreadable ({type(exc).__name__})"]
	if len(raw) > MANIFEST_SHELL_SAFETY_MAX_BYTES:
		return [f"$: manifest exceeds {MANIFEST_SHELL_SAFETY_MAX_BYTES} bytes"]
	try:
		manifest = yaml.safe_load(raw.decode("utf-8"))
	except (UnicodeDecodeError, yaml.YAMLError):
		return []
	except RecursionError:
		return ["$: manifest nesting too deep"]
	if not isinstance(manifest, dict):
		return []
	stack: list[tuple[list[Any], Any]] = []
	for key in sorted(manifest, key=str, reverse=True):
		if key == "slots" or key not in MANIFEST_SHELL_SAFETY_EXEMPT_KEYS:
			stack.append(([key], manifest[key]))
	violations: list[str] = []
	seen_containers: set[int] = set()
	while stack:
		parts, value = stack.pop()
		if isinstance(value, (dict, list)):
			# Aliases can make a container recursive or shared; check each once.
			if id(value) in seen_containers:
				continue
			seen_containers.add(id(value))
			if len(parts) > MANIFEST_SHELL_SAFETY_MAX_DEPTH:
				violations.append(
					f"{_manifest_shell_safety_pointer(parts)}: nesting exceeds {MANIFEST_SHELL_SAFETY_MAX_DEPTH} levels"
				)
				continue
		if isinstance(value, str):
			classes = _manifest_shell_unsafe_classes(value)
			if classes:
				violations.append(
					f"{_manifest_shell_safety_pointer(parts)}: contains shell-unsafe character class {','.join(classes)}"
				)
		elif isinstance(value, dict):
			for key in sorted(value, key=str, reverse=True):
				stack.append((parts + [key], value[key]))
		elif isinstance(value, list):
			for index in range(len(value) - 1, -1, -1):
				stack.append((parts + [index], value[index]))
		elif value is None or isinstance(value, (bool, int, float)):
			continue
		else:
			violations.append(
				f"{_manifest_shell_safety_pointer(parts)}: contains unsupported value type {type(value).__name__}"
			)
	return violations


# Terminal escape sequences (OSC, CSI, two-character) and the C0/C1 control
# characters other than tab and newline. Carriage returns are turned into
# newlines before this runs.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# The sandbox helper's success lines (`... selfcheck ok`, `... run ok staged`)
# always lead stderr and would fill the head-line cap. Failure lines
# (`outcome=fail reason=...`) and the provision probe lines are kept.
_SANDBOX_OK_LINE_RE = re.compile(r"^VALIDATION_HARNESS_SANDBOX\s+phase=\S+\s+outcome=ok\b")


def _credential_values_to_redact() -> list[str]:
	values = {
		os.environ.get(name, "").strip() for name in REFRESH_PIPELINE_CREDENTIAL_ENV_NAMES
	}
	# Very short values would also redact ordinary words.
	return sorted((value for value in values if len(value) >= 8), key=len, reverse=True)


def _sanitized_failure_lines(text: str | None) -> list[str]:
	"""Return the non-empty lines of consumer-controlled `text`, made log-safe.

	The text comes from the untrusted consumer clone and is printed into the
	Actions log, so: terminal escapes and control characters are removed, the
	value of every refresh credential is redacted, whitespace is collapsed,
	each line is capped at FAILURE_DETAIL_LINE_MAX_CHARS, and a line that
	starts with `::` or `##[` (a workflow command such as `::set-env` or
	`::add-mask::`) is prefixed so the runner no longer parses it.
	"""
	if not text:
		return []
	cleaned = _ANSI_ESCAPE_RE.sub("", text)
	cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
	cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
	for secret in _credential_values_to_redact():
		cleaned = cleaned.replace(secret, "[redacted]")
	lines: list[str] = []
	for raw_line in cleaned.split("\n"):
		line = " ".join(raw_line.split())
		if not line:
			continue
		if line.startswith("::") or line.startswith("##["):
			line = "[workflow-command-neutralised] " + line
		if len(line) > FAILURE_DETAIL_LINE_MAX_CHARS:
			line = line[: FAILURE_DETAIL_LINE_MAX_CHARS - 3] + "..."
		lines.append(line)
	return lines


def _tail_lines_text(lines: list[str], *, max_chars: int) -> str:
	"""Join the last FAILURE_DETAIL_TAIL_LINES of `lines` within `max_chars`.

	The newest lines win when the budget is short.
	"""
	picked: list[str] = []
	used = 0
	for line in reversed(lines[-FAILURE_DETAIL_TAIL_LINES:]):
		cost = len(line) + 4
		if picked and used + cost > max_chars:
			break
		picked.append(line)
		used += cost
	return " // ".join(reversed(picked))


def _read_log_tail(path: Path | None) -> str:
	"""Read at most FAILURE_DETAIL_FILE_READ_BYTES from the end of a log file.

	Missing, unreadable and symlinked paths give "" (the file was written by
	the sandbox copy-back, but it is still consumer-influenced).
	"""
	if path is None:
		return ""
	try:
		if path.is_symlink() or not path.is_file():
			return ""
		with path.open("rb") as handle:
			size = handle.seek(0, os.SEEK_END)
			handle.seek(max(0, size - FAILURE_DETAIL_FILE_READ_BYTES))
			return handle.read().decode("utf-8", errors="replace")
	except OSError:
		return ""


def _parse_driver_result(stdout: str | None) -> dict[str, Any] | None:
	"""Return the validation driver's result object from its stdout, if any.

	The driver prints one JSON object (`result`, `phase`, `failures[]` with
	`test` / `error` / `log_tail`). The last line that parses to an object
	wins; absent or garbled output gives None.
	"""
	if not stdout:
		return None
	for raw_line in reversed(stdout.splitlines()):
		candidate = raw_line.strip()
		if not candidate.startswith("{") or len(candidate) > 1_000_000:
			continue
		try:
			parsed = json.loads(candidate)
		except ValueError:
			continue
		if isinstance(parsed, dict):
			return parsed
	return None


def _driver_result_detail_parts(driver: dict[str, Any]) -> list[str]:
	parts: list[str] = []
	phase = driver.get("phase")
	if isinstance(phase, str):
		phase_lines = _sanitized_failure_lines(phase)
		if phase_lines:
			parts.append(f"phase={phase_lines[0]}")
	failures = driver.get("failures")
	first = failures[0] if isinstance(failures, list) and failures else None
	if not isinstance(first, dict):
		return parts
	for key in ("test", "error"):
		value = first.get(key)
		if isinstance(value, str):
			value_lines = _sanitized_failure_lines(value)
			if value_lines:
				parts.append(f"{key}={' // '.join(value_lines[:FAILURE_DETAIL_HEAD_LINES])}")
	log_tail = first.get("log_tail")
	if isinstance(log_tail, str):
		tail_text = _tail_lines_text(
			_sanitized_failure_lines(log_tail), max_chars=FAILURE_DETAIL_LOG_TAIL_MAX_CHARS
		)
		if tail_text:
			parts.append(f"log_tail={tail_text}")
	return parts


def _format_command_failure(
	stage: str,
	failure: CommandFailure,
	*,
	compose_log_path: Path | None = None,
	timeout_secs: int | None = None,
) -> str:
	"""One-line, log-safe description of a failed pipeline stage.

	Head lines come from stderr minus the sandbox helper's success lines,
	else from stdout. For the `self_test` stage, the driver's result JSON
	(phase, first failure's test / error / log_tail) and the tail of the
	copied-back compose log (`compose_log_path`) are appended, because those
	hold the real cause once the sandbox status lines own stderr.
	"""
	head_lines = [
		line
		for line in _sanitized_failure_lines(failure.stderr)
		if not _SANDBOX_OK_LINE_RE.match(line)
	]
	driver = _parse_driver_result(failure.stdout) if stage == "self_test" else None
	if not head_lines and driver is None:
		head_lines = _sanitized_failure_lines(failure.stdout)
	parts: list[str] = []
	if head_lines:
		parts.append(" | ".join(head_lines[:FAILURE_DETAIL_HEAD_LINES]))
	if failure.returncode == 124 and timeout_secs is not None:
		parts.append(f"timeout_secs={timeout_secs}")
	if stage == "self_test":
		if driver is not None:
			parts.extend(_driver_result_detail_parts(driver))
		compose_text = _tail_lines_text(
			_sanitized_failure_lines(_read_log_tail(compose_log_path)),
			max_chars=FAILURE_DETAIL_COMPOSE_TAIL_MAX_CHARS,
		)
		if compose_text:
			parts.append(f"compose_log_tail={compose_text}")
	detail = " | ".join(parts) if parts else "no_output"
	if len(detail) > FAILURE_DETAIL_MAX_CHARS:
		detail = detail[: FAILURE_DETAIL_MAX_CHARS - 3] + "..."
	return f"{stage}_failed(exit={failure.returncode}): {detail}"


if __name__ == "__main__":
	raise SystemExit(main())
