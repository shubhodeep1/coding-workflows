#!/usr/bin/env python3
"""Audit consumer workflow-wrapper drift against checked-in templates."""

from __future__ import annotations

import argparse
import base64
import difflib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow_wrapper_refs import pin_reusable_workflow_refs


REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Transient GitHub API failures (intermittent 5xx "Unicorn" HTML pages, secondary
# rate limits, and network hiccups) are retried with exponential backoff before a
# repository is declared errored, so a single flaky contents-API response no longer
# inflates the drift audit's Error count into a spurious WARNING alert.
DEFAULT_GH_API_MAX_ATTEMPTS = 4
DEFAULT_GH_API_RETRY_BASE_SECONDS = 2.0
MAX_GH_API_RETRY_DELAY_SECONDS = 30.0
# Install profiles a consumer can select with the WORKFLOW_PROFILE repository
# variable (update_workflows.yml defaults an unset variable to "full"). A
# profile that cannot be read or validated resolves to "unknown", which keeps
# the audit's previous behaviour: every template is audited (fail safe).
WORKFLOW_PROFILE_NAMES = ("core", "standard", "full")
DEFAULT_WORKFLOW_PROFILE = "full"
UNKNOWN_WORKFLOW_PROFILE = "unknown"
STANDARD_MANAGED_HEADER = (
	"# IMPORTANT: This file is managed by coding-workflows and may be overwritten",
	"# automatically when upstream templates change. To opt out of auto-updates,",
	"# set the ALLOW_WORKFLOW_EDITS repository variable to 'false'.",
)
SELF_UPDATER_MANAGED_HEADER = (
	"# IMPORTANT: This file is managed by coding-workflows and will be overwritten",
	"# by the update process. Do not add custom logic here.",
)


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
	"""Thin subprocess wrapper with structured failures."""

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
class DriftItem:
	file: str
	missing: bool
	diff_preview: str
	diff_lines_total: int


@dataclass
class RepoAuditResult:
	repository: str
	outcome: str
	drift_items: list[DriftItem] = field(default_factory=list)
	error: str | None = None
	error_detail: str | None = None
	# Templates actually compared (profile skips excluded); None on error.
	templates_compared: int | None = None


class RepoFetchError(Exception):
	"""Non-fatal per-repository fetch failure."""

	def __init__(self, repository: str, detail: str) -> None:
		super().__init__(detail)
		self.repository = repository
		self.detail = detail


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


def _remove_header_block(prefix_lines: list[str], block: tuple[str, ...]) -> list[str]:
	for index in range(0, len(prefix_lines) - len(block) + 1):
		if tuple(prefix_lines[index : index + len(block)]) == block:
			return prefix_lines[:index] + prefix_lines[index + len(block) :]
	return prefix_lines


def normalize_workflow_text(text: str) -> str:
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	lines = [line.rstrip() for line in text.split("\n")]

	prefix_end = 0
	for index, line in enumerate(lines):
		if line == "" or line.startswith("#"):
			prefix_end = index + 1
			continue
		break
	else:
		prefix_end = len(lines)

	prefix_lines = lines[:prefix_end]
	body_lines = lines[prefix_end:]
	prefix_lines = _remove_header_block(prefix_lines, STANDARD_MANAGED_HEADER)
	prefix_lines = _remove_header_block(prefix_lines, SELF_UPDATER_MANAGED_HEADER)
	while prefix_lines and prefix_lines[0] == "":
		prefix_lines.pop(0)

	normalized_lines = prefix_lines + body_lines
	while normalized_lines and normalized_lines[-1] == "":
		normalized_lines.pop()
	if not normalized_lines:
		return ""
	return "\n".join(normalized_lines) + "\n"


PROFILE_MANIFEST_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.yml$")


def _load_profile_manifest_names(templates_dir: Path) -> set[str]:
	"""Return the wrapper basenames listed in ``profiles/full.txt``.

	The manifest is the full-profile install list ``update_workflows.yml``
	uses. It also names wrappers outside the ``ai-*.yml`` glob (for example
	``review_rb_judge_dispatch.yml``). Only plain ``*.yml`` basenames are
	accepted; the caller checks that each one exists under ``templates_dir``.
	A missing or unreadable manifest yields an empty set (fail open), so the
	audit keeps its glob-only behaviour.
	"""
	# Shares one parser with the per-profile loader so the filters cannot drift.
	names = _load_named_profile_manifest_names(templates_dir, "full")
	return names if names is not None else set()


def _load_named_profile_manifest_names(templates_dir: Path, profile: str) -> set[str] | None:
	"""Return the wrapper basenames listed in ``profiles/<profile>.txt``.

	Applies the same line filters as ``_load_profile_manifest_names``. Returns
	``None`` (no manifest) when ``profile`` is not a known profile name or the
	manifest is missing or unreadable, so callers can fall back to auditing
	every template for that profile.
	"""
	if profile not in WORKFLOW_PROFILE_NAMES:
		return None
	manifest_path = templates_dir / "profiles" / f"{profile}.txt"
	try:
		manifest_text = manifest_path.read_text(encoding="utf-8")
	except (OSError, UnicodeDecodeError):
		return None

	names: set[str] = set()
	for raw_line in manifest_text.splitlines():
		name = raw_line.strip()
		if not name or name.startswith("#"):
			continue
		if "/" in name or "\\" in name or name in {".", ".."}:
			continue
		if not PROFILE_MANIFEST_NAME_RE.match(name):
			continue
		names.add(name)
	return names


def load_profile_manifests(templates_dir: Path) -> dict[str, set[str]]:
	"""Return ``{profile: wrapper names}`` for every readable, non-empty profile manifest."""
	manifests: dict[str, set[str]] = {}
	for profile in WORKFLOW_PROFILE_NAMES:
		names = _load_named_profile_manifest_names(templates_dir, profile)
		# An empty manifest counts as missing (the updater refuses it with
		# ERR_WORKFLOW_PROFILE_EMPTY), so that profile audits every template.
		if names:
			manifests[profile] = names
	return manifests


def load_expected_templates(
	templates_dir: Path,
	stable_sha: str | None = None,
	renderer: Any = None,
) -> dict[str, str]:
	if not templates_dir.is_dir():
		raise ValueError(f"templates directory not found: {templates_dir}")

	candidate_names: set[str] = {
		path.name for path in templates_dir.glob("ai-*.yml") if path.is_file()
	}
	for name in _load_profile_manifest_names(templates_dir):
		if (templates_dir / name).is_file():
			candidate_names.add(name)

	templates: dict[str, str] = {}
	for name in sorted(candidate_names):
		path = templates_dir / name
		template_text = path.read_text(encoding="utf-8")
		if stable_sha is not None:
			template_text = (renderer or pin_reusable_workflow_refs)(template_text, stable_sha)
		templates[path.name] = normalize_workflow_text(template_text)

	if not templates:
		raise ValueError(f"no ai-*.yml templates found under {templates_dir}")
	return templates


def build_diff_preview(
	*,
	file_name: str,
	expected_text: str,
	actual_text: str,
	max_diff_lines: int,
) -> tuple[str, int]:
	diff_lines = list(
		difflib.unified_diff(
			expected_text.splitlines(),
			actual_text.splitlines(),
			fromfile=f"workflow-templates/{file_name}",
			tofile=f".github/workflows/{file_name}",
			lineterm="",
		)
	)
	diff_lines_total = len(diff_lines)
	if diff_lines_total <= max_diff_lines:
		return "\n".join(diff_lines), diff_lines_total
	if max_diff_lines == 1:
		return f"... ({diff_lines_total} diff lines total; preview truncated)", diff_lines_total
	preview_lines = diff_lines[: max_diff_lines - 1]
	omitted = diff_lines_total - len(preview_lines)
	preview_lines.append(f"... ({omitted} additional diff lines omitted)")
	return "\n".join(preview_lines), diff_lines_total


def summarize_results(results: list[RepoAuditResult], template_files: list[str]) -> dict[str, Any]:
	totals = {
		"processed": len(results),
		"match": 0,
		"drift": 0,
		"error": 0,
		"drift_items": 0,
	}
	for result in results:
		if result.outcome in totals:
			totals[result.outcome] += 1
		else:
			totals["error"] += 1
		totals["drift_items"] += len(result.drift_items)

	return {
		"generated_at": datetime.now(timezone.utc).isoformat(),
		"template_files": template_files,
		"totals": totals,
		"results": [asdict(item) for item in results],
	}


def write_summary(summary: dict[str, Any], output_path: Path | None) -> None:
	if output_path is None:
		return
	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_stdout_summary(summary: dict[str, Any]) -> str:
	totals = summary.get("totals", {})
	lines = [
		"Consumer drift audit complete.",
		(
			"Processed={processed} Match={match} Drift={drift} Error={error} DriftItems={drift_items}".format(
				processed=totals.get("processed", 0),
				match=totals.get("match", 0),
				drift=totals.get("drift", 0),
				error=totals.get("error", 0),
				drift_items=totals.get("drift_items", 0),
			)
		),
	]
	for result in summary.get("results", []):
		repository = result.get("repository", "unknown")
		outcome = result.get("outcome", "unknown")
		if outcome == "drift":
			lines.append(f"- {repository}: drift ({len(result.get('drift_items', []))} file(s))")
		elif outcome == "error":
			lines.append(f"- {repository}: error ({result.get('error', 'unknown')})")
	return "\n".join(lines)


def _compact_command_output(stdout: str, stderr: str) -> str:
	parts = [part.strip() for part in (stdout, stderr) if part and part.strip()]
	return " | ".join(parts)


def _is_not_found_response(stdout: str, stderr: str) -> bool:
	combined = f"{stdout}\n{stderr}".lower()
	return (
		"http 404" in combined
		or "status code 404" in combined
		or '"message": "not found"' in combined
		or '"message":"not found"' in combined
		or combined.strip() == "not found"
	)


def _is_transient_fetch_failure(stdout: str, stderr: str, returncode: int) -> bool:
	"""Return True when a failed gh api call looks transient and worth retrying.

	Retryable signals are server-side 5xx responses (including GitHub's HTML
	"Unicorn" error page), primary/secondary rate limits, and network-layer
	hiccups. A definitive 404 (not-found) is never transient and short-circuits
	so a missing consumer repo is not retried needlessly.
	"""
	if returncode == 0:
		return False
	if _is_not_found_response(stdout, stderr):
		return False
	combined = f"{stdout}\n{stderr}".lower()
	transient_markers = (
		"http 429",
		"status code 429",
		"http 500",
		"http 502",
		"http 503",
		"http 504",
		"status code 500",
		"status code 502",
		"status code 503",
		"status code 504",
		"api rate limit exceeded",
		"primary rate limit",
		"secondary rate limit",
		"rate limit exceeded",
		"rate-limit exceeded",
		"was submitted too quickly",
		"<title>unicorn",
		"unicorn! &middot; github",
		"internal server error",
		"bad gateway",
		"service unavailable",
		"gateway timeout",
		"client.timeout exceeded",
		"connection timed out",
		"read timed out",
		"request timed out",
		"operation timed out",
		"timeout awaiting",
		"context deadline exceeded",
		"connection reset",
		"connection refused",
		"connection aborted",
		"network is unreachable",
		"could not resolve host",
		"temporary failure in name resolution",
		"no route to host",
		"eof occurred",
		"unexpected eof",
	)
	return any(marker in combined for marker in transient_markers)


def _sanitize_log_value(value: str) -> str:
	return re.sub(r"\s+", " ", value).strip()


class ConsumerDriftAuditor:
	def __init__(
		self,
		*,
		expected_templates: dict[str, str],
		max_diff_lines: int,
		executor: CommandExecutor | None = None,
		gh_api_max_attempts: int = DEFAULT_GH_API_MAX_ATTEMPTS,
		gh_api_retry_base_seconds: float = DEFAULT_GH_API_RETRY_BASE_SECONDS,
		sleeper: Any = time.sleep,
		profile_manifests: dict[str, set[str]] | None = None,
	) -> None:
		self.expected_templates = expected_templates
		self.max_diff_lines = max_diff_lines
		self.executor = executor or CommandExecutor()
		try:
			normalized_max_attempts = int(float(gh_api_max_attempts))
		except (TypeError, ValueError):
			normalized_max_attempts = DEFAULT_GH_API_MAX_ATTEMPTS
		try:
			normalized_retry_base_seconds = float(gh_api_retry_base_seconds)
		except (TypeError, ValueError):
			normalized_retry_base_seconds = DEFAULT_GH_API_RETRY_BASE_SECONDS
		self.gh_api_max_attempts = max(1, normalized_max_attempts)
		self.gh_api_retry_base_seconds = max(0.0, normalized_retry_base_seconds)
		self.sleeper = sleeper
		self.workflow_dir_listing_cache: dict[str, set[str]] = {}
		self.workflow_file_cache: dict[tuple[str, str], str | None] = {}
		# None turns profile resolution off: no variables read, profile
		# "unknown", every template audited (the pre-profile behaviour).
		self.profile_manifests = profile_manifests
		self.workflow_profile_cache: dict[str, str] = {}

	def _run_gh_api(self, endpoint: str, *, raw: bool = False) -> subprocess.CompletedProcess[str]:
		accept = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
		command = ["gh", "api", "-H", f"Accept: {accept}", endpoint]
		proc = self.executor.run(command, check=False)
		for attempt in range(1, self.gh_api_max_attempts):
			if not _is_transient_fetch_failure(proc.stdout, proc.stderr, proc.returncode):
				return proc
			delay = min(
				MAX_GH_API_RETRY_DELAY_SECONDS,
				self.gh_api_retry_base_seconds * (2 ** (attempt - 1)),
			)
			print(
				"DRIFT_SCAN_RETRY "
				f"endpoint={endpoint} attempt={attempt}/{self.gh_api_max_attempts - 1} "
				f"delay={delay:g}s detail={_sanitize_log_value(_compact_command_output(proc.stdout, proc.stderr))[:200]}"
			)
			self.sleeper(delay)
			proc = self.executor.run(command, check=False)
		return proc

	def fetch_workflow_directory_listing(self, repository: str) -> set[str]:
		cached = self.workflow_dir_listing_cache.get(repository)
		if cached is not None:
			return cached

		endpoint = f"repos/{repository}/contents/.github/workflows"
		proc = self._run_gh_api(endpoint)
		if proc.returncode != 0:
			if _is_not_found_response(proc.stdout, proc.stderr):
				self.workflow_dir_listing_cache[repository] = set()
				return self.workflow_dir_listing_cache[repository]
			detail = _compact_command_output(proc.stdout, proc.stderr) or f"gh api returned {proc.returncode}"
			raise RepoFetchError(repository, f"workflow directory listing failed: {detail}")

		try:
			payload = json.loads(proc.stdout or "[]")
		except json.JSONDecodeError as exc:
			raise RepoFetchError(repository, f"workflow directory listing returned invalid JSON: {exc}") from exc
		if not isinstance(payload, list):
			raise RepoFetchError(repository, "workflow directory listing did not return an array")

		files: set[str] = set()
		for item in payload:
			if not isinstance(item, dict):
				continue
			name = item.get("name")
			item_type = item.get("type")
			if isinstance(name, str) and item_type == "file":
				files.add(name)

		self.workflow_dir_listing_cache[repository] = files
		return files

	def fetch_workflow_profile(self, repository: str) -> str:
		"""Resolve the consumer's WORKFLOW_PROFILE; never raises.

		Returns one of WORKFLOW_PROFILE_NAMES, "full" for an unset variable
		(HTTP 404), or "unknown" for any other failure or an invalid value.
		"""
		if self.profile_manifests is None:
			return UNKNOWN_WORKFLOW_PROFILE
		cached = self.workflow_profile_cache.get(repository)
		if cached is not None:
			return cached

		# GitHub API hygiene (§14): one extra REST read per consumer per run.
		# The existing calls (the contents/.github/workflows listing and the
		# per-file raw content reads) return no repository variables. The
		# result is cached per repository, and every failure falls back to
		# "unknown", which audits every template (fail safe).
		endpoint = f"repos/{repository}/actions/variables/WORKFLOW_PROFILE"
		profile = UNKNOWN_WORKFLOW_PROFILE
		try:
			proc = self._run_gh_api(endpoint)
			if proc.returncode == 0:
				payload = json.loads(proc.stdout or "")
				value = payload.get("value") if isinstance(payload, dict) else None
				# Exact match only: update_workflows.yml uses the raw value as
				# the manifest file name, so a padded or mis-cased value (" Core ")
				# breaks the consumer's sync. Resolving it to "unknown" audits
				# every template, so such a consumer reports more drift, never less.
				if isinstance(value, str) and value in WORKFLOW_PROFILE_NAMES:
					profile = value
			elif _is_not_found_response(proc.stdout, proc.stderr):
				profile = DEFAULT_WORKFLOW_PROFILE
		except (CommandFailure, json.JSONDecodeError, ValueError):
			profile = UNKNOWN_WORKFLOW_PROFILE

		self.workflow_profile_cache[repository] = profile
		return profile

	def fetch_workflow_content(self, repository: str, file_name: str, *, available_files: set[str]) -> str | None:
		cache_key = (repository, file_name)
		if cache_key in self.workflow_file_cache:
			return self.workflow_file_cache[cache_key]
		if file_name not in available_files:
			self.workflow_file_cache[cache_key] = None
			return None

		endpoint = f"repos/{repository}/contents/.github/workflows/{file_name}"
		proc = self._run_gh_api(endpoint, raw=True)
		if proc.returncode != 0:
			if _is_not_found_response(proc.stdout, proc.stderr):
				self.workflow_file_cache[cache_key] = None
				return None
			detail = _compact_command_output(proc.stdout, proc.stderr) or f"gh api returned {proc.returncode}"
			raise RepoFetchError(repository, f"workflow fetch failed for {file_name}: {detail}")

		normalized = normalize_workflow_text(proc.stdout)
		self.workflow_file_cache[cache_key] = normalized
		return normalized

	def audit_repository(self, repository: str) -> RepoAuditResult:
		profile = self.fetch_workflow_profile(repository)
		profile_names: set[str] | None = None
		if self.profile_manifests is not None and profile != UNKNOWN_WORKFLOW_PROFILE:
			profile_names = self.profile_manifests.get(profile)
		if profile_names is not None:
			manifest_state = "loaded"
		elif self.profile_manifests is None or profile == UNKNOWN_WORKFLOW_PROFILE:
			manifest_state = "disabled"
		else:
			manifest_state = "missing"
		print(
			f"DRIFT_SCAN_START repository={repository} templates={len(self.expected_templates)} "
			f"profile={profile} manifest={manifest_state}"
		)
		try:
			available_files = self.fetch_workflow_directory_listing(repository)
			drift_items: list[DriftItem] = []
			compared_count = 0
			for file_name, expected_text in sorted(self.expected_templates.items()):
				# A wrapper the consumer's profile does not install is expected
				# to be absent. A wrapper the consumer has is always compared.
				if (
					profile_names is not None
					and file_name not in available_files
					and file_name not in profile_names
				):
					print(
						"DRIFT_SCAN_SKIP "
						f"repository={repository} file={file_name} "
						f"reason=not_in_profile profile={profile}"
					)
					continue
				compared_count += 1
				actual_text = self.fetch_workflow_content(
					repository,
					file_name,
					available_files=available_files,
				)
				if actual_text == expected_text:
					continue
				diff_preview, diff_lines_total = build_diff_preview(
					file_name=file_name,
					expected_text=expected_text,
					actual_text=actual_text or "",
					max_diff_lines=self.max_diff_lines,
				)
				missing = actual_text is None
				drift_items.append(
					DriftItem(
						file=file_name,
						missing=missing,
						diff_preview=diff_preview,
						diff_lines_total=diff_lines_total,
					)
				)
				print(
					"DRIFT_SCAN_DIFF "
					f"repository={repository} file={file_name} "
					f"missing={'true' if missing else 'false'} diff_lines_total={diff_lines_total}"
				)
		except RepoFetchError as exc:
			detail = _sanitize_log_value(exc.detail)
			print(f"DRIFT_SCAN_ERROR repository={repository} error=fetch_failed detail={detail}")
			return RepoAuditResult(
				repository=repository,
				outcome="error",
				error="fetch_failed",
				error_detail=exc.detail,
			)
		except CommandFailure as exc:
			detail = _sanitize_log_value(str(exc))
			print(f"DRIFT_SCAN_ERROR repository={repository} error=command_failed detail={detail}")
			return RepoAuditResult(
				repository=repository,
				outcome="error",
				error="command_failed",
				error_detail=str(exc),
			)

		outcome = "drift" if drift_items else "match"
		print(
			f"DRIFT_SCAN_OK repository={repository} outcome={outcome} drift_items={len(drift_items)} "
			f"compared={compared_count}"
		)
		return RepoAuditResult(
			repository=repository,
			outcome=outcome,
			drift_items=drift_items,
			templates_compared=compared_count,
		)

	def audit_repositories(self, repositories: list[str]) -> list[RepoAuditResult]:
		return [self.audit_repository(repository) for repository in repositories]


# ── Restore pull requests (daily repair) ──────────────────────────────────
#
# For each drifted consumer the audit opens, or refreshes, one pull request
# that puts every managed wrapper back to the stable release copy, byte for
# byte (the consumer's ai-workflow-integrity.yml accepts nothing else). The
# head branch is fixed and the body carries RESTORE_MARKER, so a rerun
# updates the same pull request instead of opening another. A consumer with
# an open sync pull request is skipped: that pull request already restores
# every wrapper.

RESTORE_BRANCH = "auto/restore-managed-workflows"
RESTORE_MARKER = "<!-- coding-workflows:restore-managed-workflows:v1 -->"
SYNC_PR_BRANCH_PREFIX = "auto/update-workflows-"
SYNC_FALLBACK_BRANCH = "ai-sync/stable"
INTEGRITY_WRAPPER = "ai-workflow-integrity.yml"
UPDATER_WRAPPER = "ai-update-workflows.yml"
RESTORE_COMMIT_SUBJECT = "chore: restore managed workflow wrappers to coding-workflows stable"
RESTORE_COMMIT_AUTHOR = ("coding-workflows drift audit", "41898282+github-actions[bot]@users.noreply.github.com")


def load_rendered_templates(templates_dir: Path, stable_sha: str, renderer: Any = None) -> dict[str, str]:
	"""Return ``{name: rendered text}`` for every ``*.yml`` template, unnormalized."""
	render = renderer or pin_reusable_workflow_refs
	rendered: dict[str, str] = {}
	for path in sorted(templates_dir.glob("*.yml")):
		if path.is_file():
			rendered[path.name] = render(path.read_text(encoding="utf-8"), stable_sha)
	return rendered


def load_renderer(renderer_path: Path) -> Any:
	"""Load ``pin_reusable_workflow_refs`` from a release's workflow_wrapper_refs.py."""
	spec = importlib.util.spec_from_file_location("release_workflow_wrapper_refs", renderer_path)
	if spec is None or spec.loader is None:
		raise ValueError(f"cannot load renderer: {renderer_path}")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module.pin_reusable_workflow_refs


@dataclass
class RepairResult:
	repository: str
	outcome: str
	reason: str = ""
	files: list[str] = field(default_factory=list)
	pr_number: int | None = None
	pr_url: str | None = None


class DriftRepairer:
	"""Open or refresh one restore pull request per drifted consumer.

	API budget per repository (§15): none when the cloned wrappers already
	match the release bytes; otherwise one ``GET pulls?state=open`` listing
	(one page per 100 open pull requests, at most 10; answers both "is a sync
	PR open" and "does our restore PR exist"), then at most one ``POST pulls``
	or ``PATCH pulls/<n>``. Git traffic (clone, ls-remote, fetch, push) uses the git
	protocol, not the REST API. Every failure is reported per repository and
	never stops the audit (fail open).
	"""

	def __init__(
		self,
		*,
		auditor: "ConsumerDriftAuditor",
		rendered_templates: dict[str, str],
		stable_sha: str,
		executor: CommandExecutor | None = None,
		git_base_url: str = "https://github.com",
		workdir: Path | None = None,
	) -> None:
		self.auditor = auditor
		self.rendered_templates = rendered_templates
		self.stable_sha = stable_sha
		self.executor = executor or auditor.executor
		self.git_base_url = git_base_url.rstrip("/")
		self.workdir = workdir or Path(tempfile.mkdtemp(prefix="drift-repair-"))

	def _git_env(self) -> dict[str, str]:
		# The credential travels in the environment, never on the command
		# line, so a CommandFailure message cannot leak it.
		token = os.environ.get("GH_TOKEN", "")
		if not token or not self.git_base_url.startswith("https://"):
			return {"GIT_TERMINAL_PROMPT": "0"}
		header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
		return {
			"GIT_TERMINAL_PROMPT": "0",
			"GIT_CONFIG_COUNT": "1",
			"GIT_CONFIG_KEY_0": f"http.{self.git_base_url}/.extraheader",
			"GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {header}",
		}

	def _git(self, args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
		return self.executor.run(["git", *args], cwd=cwd, check=check, env_overrides=self._git_env())

	def _open_pulls(self, repository: str) -> list[dict[str, Any]]:
		# Follows pages (at most 10) so a consumer with more than 100 open
		# pull requests still has its sync and restore pull requests found.
		pulls: list[dict[str, Any]] = []
		for page in range(1, 11):
			suffix = "" if page == 1 else f"&page={page}"
			proc = self.auditor._run_gh_api(f"repos/{repository}/pulls?state=open&per_page=100{suffix}")
			if proc.returncode != 0:
				raise RepoFetchError(repository, "open pull request listing failed: " + _compact_command_output(proc.stdout, proc.stderr))
			payload = json.loads(proc.stdout or "[]")
			if not isinstance(payload, list):
				raise RepoFetchError(repository, "open pull request listing did not return an array")
			pulls.extend(item for item in payload if isinstance(item, dict))
			if len(payload) < 100:
				return pulls
		raise RepoFetchError(repository, "open pull request listing exceeded 10 pages")

	def _managed_names(self, checkout: Path, profile_names: set[str] | None) -> list[str]:
		local_dir = checkout / ".github" / "workflows"
		# is_symlink() too: is_file() is False for a dangling symlink, which the
		# integrity check still records (and rejects) as a present entry.
		def present(name: str) -> bool:
			path = local_dir / name
			return path.is_file() or path.is_symlink()

		if profile_names is None:
			# Unknown profile: restore only wrappers the consumer already has.
			names = {name for name in self.rendered_templates if present(name)}
		else:
			names = set(profile_names)
		names.add(INTEGRITY_WRAPPER)
		# A restore pull request never adds the updater (bootstrap-only, the
		# sync never creates it). When the consumer has it, it is managed
		# whatever the profile lists: ai-workflow-integrity.yml compares it
		# whenever either tree has it.
		if present(UPDATER_WRAPPER):
			names.add(UPDATER_WRAPPER)
		else:
			names.discard(UPDATER_WRAPPER)
		return sorted(name for name in names if name in self.rendered_templates)

	def _body(self, files: list[str], version_hint: str) -> str:
		listed = "\n".join(f"- `.github/workflows/{name}`" for name in files)
		return (
			"Restores the coding-workflows managed workflow wrappers to the stable release "
			f"(`{version_hint}`).\n\n"
			"These files changed outside the coding-workflows sync. Managed wrappers are locked: "
			"`ai-workflow-integrity.yml` rejects any other content, so merge this pull request "
			"instead of editing them by hand. Changes the consumer needs belong upstream in "
			"`shubhodeep1/coding-workflows`.\n\n"
			f"Files restored:\n{listed}\n\n"
			"Opened by the daily consumer drift audit (`audit_consumer_drift.yml`). It refreshes "
			"this pull request while the drift remains.\n\n"
			f"{RESTORE_MARKER}\n"
		)

	def repair(self, repository: str, profile_names: set[str] | None) -> RepairResult:
		try:
			return self._repair(repository, profile_names)
		except (RepoFetchError, CommandFailure, json.JSONDecodeError, OSError, ValueError) as exc:
			detail = _sanitize_log_value(getattr(exc, "detail", None) or str(exc))[:300]
			print(f"DRIFT_REPAIR repository={repository} outcome=error reason=repair_failed detail={detail}")
			return RepairResult(repository=repository, outcome="error", reason="repair_failed")

	def _repair(self, repository: str, profile_names: set[str] | None) -> RepairResult:
		if profile_names is None:
			# Unknown profile (failed or invalid WORKFLOW_PROFILE read, or no
			# manifest): the consumer's integrity check would require a profile
			# manifest we cannot name, so a restore pull request could miss a
			# wrapper and fail its own status. Skip; the next daily run retries.
			print(f"DRIFT_REPAIR repository={repository} outcome=skipped reason=profile_unknown")
			return RepairResult(repository=repository, outcome="skipped", reason="profile_unknown")
		checkout = self.workdir / repository.replace("/", "__")
		if checkout.exists():
			raise ValueError(f"repair checkout already exists: {checkout}")
		self._git(["clone", "--quiet", "--depth", "1", "--no-tags", f"{self.git_base_url}/{repository}.git", str(checkout)])
		base_branch = self._git(["symbolic-ref", "--short", "HEAD"], cwd=checkout).stdout.strip()
		if not re.fullmatch(r"[A-Za-z0-9._/-]+", base_branch):
			raise ValueError("default branch name is invalid")
		# A committed symlink at .github or .github/workflows would redirect the
		# wrapper writes below outside the clone.
		if (checkout / ".github").is_symlink() or (checkout / ".github" / "workflows").is_symlink():
			print(f"DRIFT_REPAIR repository={repository} outcome=skipped reason=unsafe_checkout")
			return RepairResult(repository=repository, outcome="skipped", reason="unsafe_checkout")

		files: list[str] = []
		for name in self._managed_names(checkout, profile_names):
			target = checkout / ".github" / "workflows" / name
			expected = self.rendered_templates[name].encode("utf-8")
			if target.is_symlink() or not target.is_file() or target.read_bytes() != expected:
				if target.is_symlink():
					target.unlink()
				target.parent.mkdir(parents=True, exist_ok=True)
				target.write_bytes(expected)
				files.append(name)
		if not files:
			print(f"DRIFT_REPAIR repository={repository} outcome=skipped reason=no_exact_drift")
			return RepairResult(repository=repository, outcome="skipped", reason="no_exact_drift")

		pulls = self._open_pulls(repository)
		existing = None
		for pull in pulls:
			head = pull.get("head") or {}
			ref = str(head.get("ref") or "")
			head_repo = ((head.get("repo") or {}).get("full_name") or "")
			if head_repo != repository:
				continue
			if ref.startswith(SYNC_PR_BRANCH_PREFIX) or ref == SYNC_FALLBACK_BRANCH:
				print(f"DRIFT_REPAIR repository={repository} outcome=skipped reason=sync_pull_request_open pr={pull.get('number')}")
				return RepairResult(repository=repository, outcome="skipped", reason="sync_pull_request_open", pr_number=pull.get("number"), pr_url=pull.get("html_url"))
			if ref == RESTORE_BRANCH and RESTORE_MARKER in str(pull.get("body") or ""):
				existing = pull

		self._git(["add", "--", *[f".github/workflows/{name}" for name in files]], cwd=checkout)
		author_name, author_email = RESTORE_COMMIT_AUTHOR
		self._git(
			[
				"-c", f"user.name={author_name}", "-c", f"user.email={author_email}",
				"commit", "--quiet", "--no-verify", "-m",
				f"{RESTORE_COMMIT_SUBJECT}\n\nRelease commit: {self.stable_sha}",
			],
			cwd=checkout,
		)

		remote = self._git(["ls-remote", "--heads", "origin", f"refs/heads/{RESTORE_BRANCH}"], cwd=checkout).stdout.split()
		remote_sha = remote[0] if remote else ""
		push_needed = True
		if remote_sha:
			self._git(["fetch", "--quiet", "--depth", "1", "--no-tags", "origin", remote_sha], cwd=checkout)
			# Overwrite the fixed branch only when its tip is a restore commit
			# this audit wrote; a consumer's own branch of that name is kept.
			tip = self._git(["log", "-1", "--format=%ae%n%s", "FETCH_HEAD"], cwd=checkout).stdout.split("\n")
			if tip[:1] != [RESTORE_COMMIT_AUTHOR[1]] or not (tip[1:2] or [""])[0].startswith(RESTORE_COMMIT_SUBJECT):
				print(f"DRIFT_REPAIR repository={repository} outcome=skipped reason=restore_branch_not_owned")
				return RepairResult(repository=repository, outcome="skipped", reason="restore_branch_not_owned", files=files)
			old_tree = self._git(["rev-parse", "FETCH_HEAD^{tree}"], cwd=checkout).stdout.strip()
			new_tree = self._git(["rev-parse", "HEAD^{tree}"], cwd=checkout).stdout.strip()
			push_needed = old_tree != new_tree
		if push_needed:
			self._git(
				["push", "--quiet", f"--force-with-lease=refs/heads/{RESTORE_BRANCH}:{remote_sha}", "origin", f"HEAD:refs/heads/{RESTORE_BRANCH}"],
				cwd=checkout,
			)

		title = "Restore coding-workflows managed workflow wrappers"
		body = self._body(files, self.stable_sha[:12])
		if existing is not None:
			number = existing.get("number")
			if not push_needed and str(existing.get("body") or "") == body:
				print(f"DRIFT_REPAIR repository={repository} outcome=unchanged reason=up_to_date pr={number} files={','.join(files)}")
				return RepairResult(repository=repository, outcome="unchanged", reason="up_to_date", files=files, pr_number=number, pr_url=existing.get("html_url"))
			proc = self.executor.run(
				["gh", "api", "-X", "PATCH", f"repos/{repository}/pulls/{number}", "-f", f"title={title}", "-f", f"body={body}"],
				check=False,
			)
			if proc.returncode != 0:
				raise RepoFetchError(repository, "restore pull request update failed: " + _compact_command_output(proc.stdout, proc.stderr))
			print(f"DRIFT_REPAIR repository={repository} outcome=updated reason=drift pr={number} files={','.join(files)}")
			return RepairResult(repository=repository, outcome="updated", reason="drift", files=files, pr_number=number, pr_url=existing.get("html_url"))

		proc = self.executor.run(
			[
				"gh", "api", "-X", "POST", f"repos/{repository}/pulls",
				"-f", f"title={title}", "-f", f"head={RESTORE_BRANCH}", "-f", f"base={base_branch}", "-f", f"body={body}",
			],
			check=False,
		)
		if proc.returncode != 0:
			raise RepoFetchError(repository, "restore pull request creation failed: " + _compact_command_output(proc.stdout, proc.stderr))
		created = json.loads(proc.stdout or "{}")
		number = created.get("number") if isinstance(created, dict) else None
		url = created.get("html_url") if isinstance(created, dict) else None
		print(f"DRIFT_REPAIR repository={repository} outcome=created reason=drift pr={number} files={','.join(files)}")
		return RepairResult(repository=repository, outcome="created", reason="drift", files=files, pr_number=number, pr_url=url)


def repair_drifted_repositories(
	auditor: ConsumerDriftAuditor,
	repairer: DriftRepairer,
	results: list[RepoAuditResult],
) -> list[RepairResult]:
	"""Run the repairer for every drifted or matching repository; errors are skipped.

	The audit compares normalized text (managed header, trailing whitespace and
	CRLF ignored) while ai-workflow-integrity.yml accepts only exact bytes, so a
	"match" can still fail the integrity check. Matching repositories are
	therefore byte-checked too (one clone, no REST call when the bytes match);
	their ``no_exact_drift`` and ``profile_unknown`` skips are left out of
	the summary.
	"""
	repairs: list[RepairResult] = []
	for result in results:
		if result.outcome not in ("drift", "match"):
			continue
		profile = auditor.fetch_workflow_profile(result.repository)
		profile_names = None
		if auditor.profile_manifests is not None and profile != UNKNOWN_WORKFLOW_PROFILE:
			profile_names = auditor.profile_manifests.get(profile)
		repair = repairer.repair(result.repository, profile_names)
		if result.outcome == "match" and repair.outcome == "skipped" and repair.reason in ("no_exact_drift", "profile_unknown"):
			continue
		repairs.append(repair)
	return repairs


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--consumer-repos",
		type=Path,
		default=Path(".github/ai/consumer_repos.json"),
		help="JSON registry containing consumer repositories as owner/repo strings",
	)
	parser.add_argument(
		"--templates-dir",
		type=Path,
		default=Path("workflow-templates"),
		help="Directory containing canonical ai-*.yml workflow templates",
	)
	parser.add_argument(
		"--stable-sha",
		required=True,
		help="Peeled 40-character commit SHA for the current stable release",
	)
	parser.add_argument(
		"--output",
		type=Path,
		default=None,
		help="Optional path for the machine-readable JSON summary",
	)
	parser.add_argument(
		"--max-diff-lines",
		type=int,
		default=25,
		help="Maximum number of diff-preview lines to retain per file",
	)
	parser.add_argument(
		"--repair",
		action="store_true",
		help="Open or refresh a restore pull request in every drifted repository",
	)
	parser.add_argument(
		"--renderer",
		type=Path,
		default=None,
		help="workflow_wrapper_refs.py of the release the templates come from (default: the local copy)",
	)
	parser.add_argument(
		"--git-base-url",
		default="https://github.com",
		help="Git host base URL for restore pull request clones and pushes",
	)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	if args.max_diff_lines < 1:
		print("ERROR: --max-diff-lines must be at least 1", file=sys.stderr)
		return 1

	try:
		repositories = load_target_repositories(args.consumer_repos)
		renderer = load_renderer(args.renderer) if args.renderer is not None else None
		expected_templates = load_expected_templates(args.templates_dir, args.stable_sha, renderer)
		rendered_templates = load_rendered_templates(args.templates_dir, args.stable_sha, renderer) if args.repair else {}
	except (OSError, ValueError, AttributeError) as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 1

	auditor = ConsumerDriftAuditor(
		expected_templates=expected_templates,
		max_diff_lines=args.max_diff_lines,
		profile_manifests=load_profile_manifests(args.templates_dir),
	)
	results = auditor.audit_repositories(repositories)
	summary = summarize_results(results, sorted(expected_templates))
	if args.repair:
		repairer = DriftRepairer(
			auditor=auditor,
			rendered_templates=rendered_templates,
			stable_sha=args.stable_sha.lower(),
			git_base_url=args.git_base_url,
		)
		summary["repairs"] = [asdict(item) for item in repair_drifted_repositories(auditor, repairer, results)]

	try:
		write_summary(summary, args.output)
	except OSError as exc:
		print(f"ERROR: failed to write summary JSON: {exc}", file=sys.stderr)
		return 1

	print(render_stdout_summary(summary))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
