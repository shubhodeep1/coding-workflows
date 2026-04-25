#!/usr/bin/env python3
"""Run validation harness self-tests across fixture workspaces."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple


SCHEMA_VERSION = "1"


class FixtureSpec(NamedTuple):
	name: str
	source_path: Path
	manifest_path: Path
	legacy_manifest: bool = False


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Run validation harness self-tests for fixture workspaces.")
	parser.add_argument(
		"--repo-root",
		default=".",
		help="Repository root used for resolving relative paths.",
	)
	parser.add_argument(
		"--fixtures-root",
		default="tests/fixtures/selftest",
		help="Directory containing fixture workspaces.",
	)
	parser.add_argument(
		"--summary-path",
		default="artifacts/validation-selftest-summary.json",
		help="Path for machine-readable JSON summary output.",
	)
	parser.add_argument(
		"--log-dir",
		default="artifacts/validation-selftest-logs",
		help="Directory for per-fixture stage logs.",
	)
	parser.add_argument(
		"--runtime-command",
		default="bash scripts/validate_driver.sh",
		help="Runtime validation command executed in each prepared fixture workspace.",
	)
	parser.add_argument(
		"--runtime-timeout-seconds",
		type=int,
		default=1800,
		help="Timeout in seconds for the runtime validation command (default: 1800).",
	)
	parser.add_argument(
		"--skip-compose-config",
		action="store_true",
		help="Legacy no-op retained for backward compatibility.",
	)
	return parser


def _resolve_path(repo_root: Path, value: str) -> Path:
	candidate = Path(value)
	if candidate.is_absolute():
		return candidate.resolve()
	return (repo_root / candidate).resolve()


def _repo_rel(path: Path, repo_root: Path) -> str:
	try:
		return path.resolve().relative_to(repo_root.resolve()).as_posix()
	except ValueError:
		return path.resolve().as_posix()


def _utc_timestamp() -> str:
	return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fixture_log_name(fixture_name: str) -> str:
	safe = fixture_name.strip().replace("/", "__")
	safe = safe.replace("\\", "__")
	if not safe:
		return "fixture"
	return safe


def _discover_fixtures(fixtures_root: Path) -> list[FixtureSpec]:
	if not fixtures_root.exists() or not fixtures_root.is_dir():
		return []

	fixtures_root_resolved = fixtures_root.resolve()
	fixture_dirs = sorted(
		[
			child.resolve()
			for child in fixtures_root.iterdir()
			if child.is_dir() and not child.name.startswith(".")
		]
	)
	if fixture_dirs:
		return [
			FixtureSpec(
				name=fixture_dir.name,
				source_path=fixture_dir,
				manifest_path=fixture_dir / ".ai" / "validate.yml",
				legacy_manifest=False,
			)
			for fixture_dir in fixture_dirs
		]

	manifest_paths = sorted(fixtures_root.glob("*.yml")) + sorted(fixtures_root.glob("*.yaml"))
	discovered: list[FixtureSpec] = []
	for manifest_path in manifest_paths:
		if not manifest_path.is_file():
			continue
		resolved = manifest_path.resolve()
		try:
			resolved.relative_to(fixtures_root_resolved)
		except ValueError:
			continue
		discovered.append(
			FixtureSpec(
				name=manifest_path.name,
				source_path=resolved.parent,
				manifest_path=resolved,
				legacy_manifest=True,
			)
		)
	return discovered


def _write_command_log(
	log_path: Path,
	command: list[str],
	cwd: Path,
	result: subprocess.CompletedProcess[str],
	duration: float,
	*,
	env_overrides: dict[str, str] | None = None,
) -> None:
	log_path.parent.mkdir(parents=True, exist_ok=True)
	payload = [
		f"command: {shlex.join(command)}",
		f"cwd: {cwd.as_posix()}",
		f"exit_code: {result.returncode}",
		f"duration_seconds: {duration:.3f}",
	]
	if env_overrides:
		payload.append(f"env_overrides: {json.dumps(env_overrides, sort_keys=True)}")
	payload.extend(
		[
			"--- stdout ---",
			result.stdout,
			"--- stderr ---",
			result.stderr,
		]
	)
	try:
		log_path.write_text("\n".join(payload), encoding="utf-8")
	except OSError as exc:
		print(f"validation-selftest: unable to write log {log_path}: {exc}", file=sys.stderr)


def _run_command(
	command: list[str],
	cwd: Path,
	log_path: Path,
	repo_root: Path,
	*,
	timeout_seconds: int = 300,
	env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
	started = time.monotonic()
	run_env = os.environ.copy()
	if env_overrides:
		run_env.update(env_overrides)
	try:
		result = subprocess.run(
			command,
			cwd=str(cwd),
			text=True,
			capture_output=True,
			check=False,
			timeout=timeout_seconds,
			env=run_env,
		)
	except FileNotFoundError as exc:
		result = subprocess.CompletedProcess(command, 127, stdout="", stderr=f"{exc.__class__.__name__}: {exc}")
	except subprocess.TimeoutExpired as exc:
		stdout = exc.stdout if isinstance(exc.stdout, str) else ""
		stderr_tail = exc.stderr if isinstance(exc.stderr, str) else ""
		if isinstance(exc.stdout, bytes):
			stdout = exc.stdout.decode("utf-8", errors="replace")
		if isinstance(exc.stderr, bytes):
			stderr_tail = exc.stderr.decode("utf-8", errors="replace")
		stderr = f"{exc.__class__.__name__}: {exc}"
		if stderr_tail:
			stderr = f"{stderr}\n{stderr_tail}"
		result = subprocess.CompletedProcess(command, 124, stdout=stdout, stderr=stderr)
	except Exception as exc:
		result = subprocess.CompletedProcess(command, 1, stdout="", stderr=f"{exc.__class__.__name__}: {exc}")
	duration = time.monotonic() - started
	_write_command_log(log_path, command, cwd, result, duration, env_overrides=env_overrides)
	return {
		"status": "pass" if result.returncode == 0 else "fail",
		"exit_code": result.returncode,
		"duration_seconds": round(duration, 3),
		"log_path": _repo_rel(log_path, repo_root),
	}


def _write_prepare_log(log_path: Path, source_path: Path, workspace_path: Path, manifest_path: Path, error: str | None = None) -> None:
	command = ["prepare_fixture_workspace", source_path.as_posix(), workspace_path.as_posix()]
	if error:
		result = subprocess.CompletedProcess(command, 1, stdout="", stderr=error)
	else:
		result = subprocess.CompletedProcess(
			command,
			0,
			stdout=(
				f"source_path: {source_path.as_posix()}\n"
				f"workspace_path: {workspace_path.as_posix()}\n"
				f"manifest_path: {manifest_path.as_posix()}"
			),
			stderr="",
		)
	_write_command_log(log_path, command, workspace_path.parent, result, 0.0)


def _skipped_stage(reason: str) -> dict[str, Any]:
	return {
		"status": "skipped",
		"reason": reason,
	}


def _stage_clone(
	repo_root: Path,
	fixture: FixtureSpec,
	fixture_log_dir: Path,
) -> tuple[dict[str, Any], Path | None, Path | None]:
	log_path = fixture_log_dir / "clone.log"
	workspace_root = fixture_log_dir / "workspace"
	if workspace_root.exists():
		shutil.rmtree(workspace_root)

	if not fixture.source_path.exists():
		error = f"Fixture source path does not exist: {fixture.source_path.as_posix()}"
		_write_prepare_log(log_path, fixture.source_path, workspace_root, fixture.manifest_path, error=error)
		return {
			"status": "fail",
			"error": error,
			"log_path": _repo_rel(log_path, repo_root),
		}, None, None

	if not fixture.source_path.is_dir() and not fixture.legacy_manifest:
		error = f"Fixture source path is not a directory: {fixture.source_path.as_posix()}"
		_write_prepare_log(log_path, fixture.source_path, workspace_root, fixture.manifest_path, error=error)
		return {
			"status": "fail",
			"error": error,
			"log_path": _repo_rel(log_path, repo_root),
		}, None, None

	started = time.monotonic()
	try:
		if fixture.legacy_manifest:
			workspace_manifest = workspace_root / ".ai" / "validate.yml"
			workspace_manifest.parent.mkdir(parents=True, exist_ok=True)
			if not fixture.manifest_path.exists():
				raise FileNotFoundError(f"Fixture manifest missing: {fixture.manifest_path.as_posix()}")
			shutil.copy2(fixture.manifest_path, workspace_manifest)
		else:
			if not fixture.manifest_path.exists():
				raise FileNotFoundError(f"Fixture manifest missing: {fixture.manifest_path.as_posix()}")
			shutil.copytree(
				fixture.source_path,
				workspace_root,
				dirs_exist_ok=False,
				ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
			)
			workspace_manifest = workspace_root / ".ai" / "validate.yml"
			if not workspace_manifest.exists():
				raise FileNotFoundError(
					f"Prepared workspace is missing .ai/validate.yml: {workspace_manifest.as_posix()}"
				)
	except (OSError, shutil.Error, FileNotFoundError) as exc:
		duration = time.monotonic() - started
		_write_prepare_log(log_path, fixture.source_path, workspace_root, fixture.manifest_path, error=str(exc))
		return {
			"status": "fail",
			"error": str(exc),
			"duration_seconds": round(duration, 3),
			"log_path": _repo_rel(log_path, repo_root),
		}, None, None

	duration = time.monotonic() - started
	_write_prepare_log(log_path, fixture.source_path, workspace_root, workspace_manifest)
	return {
		"status": "pass",
		"duration_seconds": round(duration, 3),
		"log_path": _repo_rel(log_path, repo_root),
		"workspace_path": _repo_rel(workspace_root, repo_root),
		"workspace_manifest_path": _repo_rel(workspace_manifest, repo_root),
	}, workspace_root, workspace_manifest


def _stage_render(repo_root: Path, manifest_path: Path, output_root: Path, fixture_log_dir: Path) -> dict[str, Any]:
	log_path = fixture_log_dir / "render.log"
	if output_root.exists():
		shutil.rmtree(output_root)
	output_root.mkdir(parents=True, exist_ok=True)
	command = [
		sys.executable,
		str(repo_root / "scripts" / "render_validation_templates.py"),
		"--manifest",
		str(manifest_path),
		"--schema",
		str(repo_root / "scripts" / "templates" / "slot_manifest.schema.json"),
		"--templates-root",
		str(repo_root / "workflow-templates" / "validation-harness"),
		"--output-root",
		str(output_root),
	]
	return _run_command(command, output_root.parent, log_path, repo_root)


def _stage_lint(repo_root: Path, output_root: Path, fixture_log_dir: Path) -> dict[str, Any]:
	log_path = fixture_log_dir / "lint.log"
	command = [
		sys.executable,
		str(repo_root / "scripts" / "validation_lint.py"),
		str(output_root),
	]
	return _run_command(command, output_root.parent, log_path, repo_root)


def _stage_runtime(
	repo_root: Path,
	workspace_root: Path,
	fixture_log_dir: Path,
	*,
	runtime_command: str,
	runtime_timeout_seconds: int,
) -> dict[str, Any]:
	log_path = fixture_log_dir / "runtime.log"
	command = shlex.split(runtime_command)
	if not command:
		_write_prepare_log(
			log_path,
			workspace_root,
			workspace_root,
			workspace_root / ".ai" / "validate.yml",
			error="Runtime command is empty",
		)
		return {
			"status": "fail",
			"error": "Runtime command is empty",
			"log_path": _repo_rel(log_path, repo_root),
		}

	def _resolve_command_path(token: str) -> str:
		token_path = Path(token)
		if token_path.is_absolute():
			return token
		workspace_candidate = workspace_root / token_path
		if workspace_candidate.exists():
			return token
		repo_candidate = repo_root / token_path
		if repo_candidate.exists():
			return str(repo_candidate)
		return token

	resolved_command = list(command)
	if Path(resolved_command[0]).name in {"bash", "sh"} and len(resolved_command) >= 2:
		resolved_command[1] = _resolve_command_path(resolved_command[1])
	else:
		resolved_command[0] = _resolve_command_path(resolved_command[0])

	env_overrides = {
		"VALIDATE_ENV_FILE": "validation/validate.env",
		"COMPOSE_FILE": "validation/docker-compose.test.yml",
		"TEST_DIR": "validation/tests",
		"LOG_DIR": str((workspace_root / "validation" / "logs").resolve()),
	}
	return _run_command(
		resolved_command,
		workspace_root,
		log_path,
		repo_root,
		timeout_seconds=runtime_timeout_seconds,
		env_overrides=env_overrides,
	)


def _run_fixture(
	repo_root: Path,
	fixture: FixtureSpec,
	logs_root: Path,
	*,
	runtime_command: str,
	runtime_timeout_seconds: int,
) -> dict[str, Any]:
	fixture_log_dir = logs_root / _fixture_log_name(fixture.name)
	fixture_started = time.monotonic()
	stages: dict[str, dict[str, Any]] = {}

	clone_stage, workspace_root, workspace_manifest = _stage_clone(
		repo_root=repo_root,
		fixture=fixture,
		fixture_log_dir=fixture_log_dir,
	)
	stages["clone"] = clone_stage

	output_root: Path | None = None
	if clone_stage["status"] == "pass" and workspace_root is not None and workspace_manifest is not None:
		output_root = workspace_root / "validation"
		render = _stage_render(repo_root, workspace_manifest, output_root, fixture_log_dir)
		stages["render"] = render
	else:
		stages["render"] = _skipped_stage("clone_failed")

	if stages["render"]["status"] == "pass" and output_root is not None:
		lint = _stage_lint(repo_root, output_root, fixture_log_dir)
		stages["lint"] = lint
	else:
		stages["lint"] = _skipped_stage("render_failed")

	if stages["render"]["status"] == "pass" and stages["lint"]["status"] == "pass" and workspace_root is not None:
		stages["runtime"] = _stage_runtime(
			repo_root,
			workspace_root,
			fixture_log_dir,
			runtime_command=runtime_command,
			runtime_timeout_seconds=runtime_timeout_seconds,
		)
	else:
		stages["runtime"] = _skipped_stage("prior_stage_failed")

	fixture_status = "pass"
	for stage_name in ("clone", "render", "lint", "runtime"):
		if stages[stage_name]["status"] == "fail":
			fixture_status = "fail"
			break

	fixture_duration = time.monotonic() - fixture_started
	log_paths = {
		stage_name: stage_payload["log_path"]
		for stage_name, stage_payload in stages.items()
		if "log_path" in stage_payload
	}

	result: dict[str, Any] = {
		"name": fixture.name,
		"fixture_path": _repo_rel(fixture.source_path, repo_root),
		"manifest_path": _repo_rel(fixture.manifest_path, repo_root),
		"status": fixture_status,
		"duration_seconds": round(fixture_duration, 3),
		"stages": stages,
		"log_paths": log_paths,
	}
	if workspace_root is not None:
		result["workspace_path"] = _repo_rel(workspace_root, repo_root)
	if output_root is not None:
		result["output_root"] = _repo_rel(output_root, repo_root)
	return result


def _build_summary(repo_root: Path, fixture_results: list[dict[str, Any]]) -> dict[str, Any]:
	failed = [item for item in fixture_results if item["status"] != "pass"]
	return {
		"schema_version": SCHEMA_VERSION,
		"generated_at": _utc_timestamp(),
		"overall_status": "pass" if not failed else "fail",
		"totals": {
			"fixtures": len(fixture_results),
			"passed": len(fixture_results) - len(failed),
			"failed": len(failed),
		},
		"fixtures": fixture_results,
		"repo_root": _repo_rel(repo_root, repo_root),
	}


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	repo_root = _resolve_path(Path.cwd(), args.repo_root)
	fixtures_root = _resolve_path(repo_root, args.fixtures_root)
	summary_path = _resolve_path(repo_root, args.summary_path)
	logs_root = _resolve_path(repo_root, args.log_dir)

	if args.skip_compose_config:
		print(
			"validation-selftest: --skip-compose-config is deprecated and ignored in runtime mode",
			file=sys.stderr,
		)

	runtime_timeout_seconds = args.runtime_timeout_seconds
	if runtime_timeout_seconds <= 0:
		runtime_timeout_seconds = 1800

	fixtures = _discover_fixtures(fixtures_root)
	logs_root.mkdir(parents=True, exist_ok=True)

	fixture_results: list[dict[str, Any]] = []
	if not fixtures:
		summary = {
			"schema_version": SCHEMA_VERSION,
			"generated_at": _utc_timestamp(),
			"overall_status": "fail",
			"totals": {
				"fixtures": 0,
				"passed": 0,
				"failed": 0,
			},
			"fixtures": [],
			"repo_root": _repo_rel(repo_root, repo_root),
			"error": (
				"No fixture workspaces or manifests found in "
				f"{fixtures_root.as_posix()} (expected fixture directories containing .ai/validate.yml)"
			),
		}
		summary_path.parent.mkdir(parents=True, exist_ok=True)
		summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
		print(summary["error"], file=sys.stderr)
		return 1

	for fixture in fixtures:
		fixture_results.append(
			_run_fixture(
				repo_root=repo_root,
				fixture=fixture,
				logs_root=logs_root,
				runtime_command=args.runtime_command,
				runtime_timeout_seconds=runtime_timeout_seconds,
			)
		)

	summary = _build_summary(repo_root, fixture_results)
	summary_path.parent.mkdir(parents=True, exist_ok=True)
	summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

	print(
		f"validation-selftest: fixtures={summary['totals']['fixtures']} "
		f"passed={summary['totals']['passed']} failed={summary['totals']['failed']} "
		f"summary={summary_path.as_posix()}"
	)
	return 0 if summary["overall_status"] == "pass" else 1


if __name__ == "__main__":
	raise SystemExit(main())
