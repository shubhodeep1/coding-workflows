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


REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


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
				timeout=300,
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


class ValidationRefreshRunner:
	"""Renders validation assets in a temp clone and reports drift; never pushes."""

	def __init__(
		self,
		*,
		source_root: Path,
		branch_name: str,
		commit_message: str = "",
		pr_title: str = "",
		executor: CommandExecutor | None = None,
	) -> None:
		self.source_root = source_root
		self.branch_name = branch_name
		# `commit_message` and `pr_title` are accepted for backward compatibility
		# with the previous PR-creating runner; this runner no longer commits,
		# pushes, or opens pull requests, so the values are ignored.
		self.commit_message = commit_message
		self.pr_title = pr_title
		self.executor = executor or CommandExecutor()

	def run_repositories(self, repositories: list[str], workspace_root: Path) -> list[RefreshResult]:
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
				result.outcome = "error"
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
		remote_branch = self.executor.run(
			["git", "ls-remote", "--heads", "origin", f"refs/heads/{self.branch_name}"],
			cwd=repo_dir,
		)
		start_ref = f"origin/{self.branch_name}" if (remote_branch.stdout or "").strip() else f"origin/{default_branch}"
		self.executor.run(["git", "fetch", "origin", default_branch], cwd=repo_dir)
		if start_ref == f"origin/{self.branch_name}":
			self.executor.run(["git", "fetch", "origin", self.branch_name], cwd=repo_dir)
		self.executor.run(["git", "checkout", "-B", self.branch_name, start_ref], cwd=repo_dir)

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
		summary = summarize_results(results)
		write_summary(summary, args.summary_json)
		print(json.dumps(summary, sort_keys=True))
	finally:
		if temporary_root is not None:
			temporary_root.cleanup()

	return 1 if any(result.outcome == "error" for result in results) else 0


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
