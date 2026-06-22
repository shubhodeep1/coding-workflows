#!/usr/bin/env python3
"""Audit consumer workflow-wrapper drift against checked-in templates."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
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


def load_expected_templates(templates_dir: Path) -> dict[str, str]:
	if not templates_dir.is_dir():
		raise ValueError(f"templates directory not found: {templates_dir}")

	templates: dict[str, str] = {}
	for path in sorted(templates_dir.glob("ai-*.yml")):
		if not path.is_file():
			continue
		templates[path.name] = normalize_workflow_text(path.read_text(encoding="utf-8"))

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


def _sanitize_log_value(value: str) -> str:
	return re.sub(r"\s+", " ", value).strip()


class ConsumerDriftAuditor:
	def __init__(
		self,
		*,
		expected_templates: dict[str, str],
		max_diff_lines: int,
		executor: CommandExecutor | None = None,
	) -> None:
		self.expected_templates = expected_templates
		self.max_diff_lines = max_diff_lines
		self.executor = executor or CommandExecutor()
		self.workflow_dir_listing_cache: dict[str, set[str]] = {}
		self.workflow_file_cache: dict[tuple[str, str], str | None] = {}

	def _run_gh_api(self, endpoint: str, *, raw: bool = False) -> subprocess.CompletedProcess[str]:
		accept = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
		return self.executor.run(
			["gh", "api", "-H", f"Accept: {accept}", endpoint],
			check=False,
		)

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
		print(f"DRIFT_SCAN_START repository={repository} templates={len(self.expected_templates)}")
		try:
			available_files = self.fetch_workflow_directory_listing(repository)
			drift_items: list[DriftItem] = []
			for file_name, expected_text in sorted(self.expected_templates.items()):
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
		print(f"DRIFT_SCAN_OK repository={repository} outcome={outcome} drift_items={len(drift_items)}")
		return RepoAuditResult(repository=repository, outcome=outcome, drift_items=drift_items)

	def audit_repositories(self, repositories: list[str]) -> list[RepoAuditResult]:
		return [self.audit_repository(repository) for repository in repositories]


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
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	if args.max_diff_lines < 1:
		print("ERROR: --max-diff-lines must be at least 1", file=sys.stderr)
		return 1

	try:
		repositories = load_target_repositories(args.consumer_repos)
		expected_templates = load_expected_templates(args.templates_dir)
	except (OSError, ValueError) as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 1

	auditor = ConsumerDriftAuditor(
		expected_templates=expected_templates,
		max_diff_lines=args.max_diff_lines,
	)
	results = auditor.audit_repositories(repositories)
	summary = summarize_results(results, sorted(expected_templates))

	try:
		write_summary(summary, args.output)
	except OSError as exc:
		print(f"ERROR: failed to write summary JSON: {exc}", file=sys.stderr)
		return 1

	print(render_stdout_summary(summary))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
