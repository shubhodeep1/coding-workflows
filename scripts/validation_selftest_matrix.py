#!/usr/bin/env python3
"""Run validation harness self-tests across fixture manifests."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1"


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Run validation harness self-tests for fixture manifests.")
	parser.add_argument(
		"--repo-root",
		default=".",
		help="Repository root used for resolving relative paths.",
	)
	parser.add_argument(
		"--fixtures-root",
		default="examples/validation-fixtures",
		help="Directory containing fixture manifests.",
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
		"--skip-compose-config",
		action="store_true",
		help="Skip docker compose config sanity check.",
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


def _discover_fixtures(fixtures_root: Path) -> list[Path]:
	fixtures_root_resolved = fixtures_root.resolve()
	manifests = sorted(fixtures_root.glob("*.yml")) + sorted(fixtures_root.glob("*.yaml"))
	filtered: list[Path] = []
	for path in manifests:
		if not path.is_file():
			continue
		resolved = path.resolve()
		try:
			resolved.relative_to(fixtures_root_resolved)
		except ValueError:
			continue
		filtered.append(resolved)
	return sorted(set(filtered))


def _write_command_log(log_path: Path, command: list[str], cwd: Path, result: subprocess.CompletedProcess[str], duration: float) -> None:
	log_path.parent.mkdir(parents=True, exist_ok=True)
	payload = [
		f"command: {shlex.join(command)}",
		f"cwd: {cwd.as_posix()}",
		f"exit_code: {result.returncode}",
		f"duration_seconds: {duration:.3f}",
		"--- stdout ---",
		result.stdout,
		"--- stderr ---",
		result.stderr,
	]
	try:
		log_path.write_text("\n".join(payload), encoding="utf-8")
	except OSError as exc:
		print(f"validation-selftest: unable to write log {log_path}: {exc}", file=sys.stderr)


def _run_command(command: list[str], cwd: Path, log_path: Path, repo_root: Path) -> dict[str, Any]:
	started = time.monotonic()
	try:
		result = subprocess.run(
			command,
			cwd=str(cwd),
			text=True,
			capture_output=True,
			check=False,
			timeout=300,
		)
	except FileNotFoundError as exc:
		result = subprocess.CompletedProcess(command, 127, stdout="", stderr=f"{exc.__class__.__name__}: {exc}")
	except subprocess.TimeoutExpired as exc:
		result = subprocess.CompletedProcess(command, 124, stdout="", stderr=f"{exc.__class__.__name__}: {exc}")
	except Exception as exc:
		result = subprocess.CompletedProcess(command, 1, stdout="", stderr=f"{exc.__class__.__name__}: {exc}")
	duration = time.monotonic() - started
	_write_command_log(log_path, command, cwd, result, duration)
	return {
		"status": "pass" if result.returncode == 0 else "fail",
		"exit_code": result.returncode,
		"duration_seconds": round(duration, 3),
		"log_path": _repo_rel(log_path, repo_root),
	}


def _skipped_stage(reason: str) -> dict[str, Any]:
	return {
		"status": "skipped",
		"reason": reason,
	}


def _stage_render(repo_root: Path, manifest_path: Path, output_root: Path, fixture_log_dir: Path) -> dict[str, Any]:
	log_path = fixture_log_dir / "render.log"
	command = [
		sys.executable,
		"scripts/render_validation_templates.py",
		"--manifest",
		str(manifest_path),
		"--schema",
		"scripts/templates/slot_manifest.schema.json",
		"--templates-root",
		"workflow-templates/validation-harness",
		"--output-root",
		str(output_root),
	]
	return _run_command(command, repo_root, log_path, repo_root)


def _stage_lint(repo_root: Path, output_root: Path, fixture_log_dir: Path) -> dict[str, Any]:
	log_path = fixture_log_dir / "lint.log"
	command = [
		sys.executable,
		"scripts/validation_lint.py",
		str(output_root),
	]
	return _run_command(command, repo_root, log_path, repo_root)


def _run_sanity_check(command: list[str], cwd: Path) -> tuple[int, str, str, float]:
	started = time.monotonic()
	try:
		result = subprocess.run(
			command,
			cwd=str(cwd),
			text=True,
			capture_output=True,
			check=False,
			timeout=30,
		)
		duration = time.monotonic() - started
		return result.returncode, result.stdout, result.stderr, duration
	except FileNotFoundError as exc:
		duration = time.monotonic() - started
		return 127, "", str(exc), duration
	except subprocess.TimeoutExpired as exc:
		duration = time.monotonic() - started
		return 124, "", str(exc), duration
	except Exception as exc:
		duration = time.monotonic() - started
		return 1, "", str(exc), duration


def _stage_sanity(repo_root: Path, output_root: Path, fixture_log_dir: Path, skip_compose_config: bool) -> dict[str, Any]:
	log_path = fixture_log_dir / "sanity.log"
	checks: list[dict[str, Any]] = []
	log_sections: list[str] = []
	overall_status = "pass"
	stage_started = time.monotonic()

	for shell_file in sorted(output_root.rglob("*.sh")):
		command = ["bash", "-n", str(shell_file)]
		exit_code, stdout, stderr, duration = _run_sanity_check(command, repo_root)
		status = "pass" if exit_code == 0 else "fail"
		if status == "fail":
			overall_status = "fail"
		checks.append(
			{
				"name": f"bash_syntax:{_repo_rel(shell_file, repo_root)}",
				"status": status,
				"exit_code": exit_code,
				"duration_seconds": round(duration, 3),
			}
		)
		log_sections.extend(
			[
				f"check: bash_syntax:{_repo_rel(shell_file, repo_root)}",
				f"command: {shlex.join(command)}",
				f"exit_code: {exit_code}",
				f"duration_seconds: {duration:.3f}",
				"--- stdout ---",
				stdout,
				"--- stderr ---",
				stderr,
				"",
			]
		)

	for python_file in sorted(output_root.rglob("*.py")):
		command = [sys.executable, "-m", "py_compile", str(python_file)]
		exit_code, stdout, stderr, duration = _run_sanity_check(command, repo_root)
		status = "pass" if exit_code == 0 else "fail"
		if status == "fail":
			overall_status = "fail"
		checks.append(
			{
				"name": f"python_compile:{_repo_rel(python_file, repo_root)}",
				"status": status,
				"exit_code": exit_code,
				"duration_seconds": round(duration, 3),
			}
		)
		log_sections.extend(
			[
				f"check: python_compile:{_repo_rel(python_file, repo_root)}",
				f"command: {shlex.join(command)}",
				f"exit_code: {exit_code}",
				f"duration_seconds: {duration:.3f}",
				"--- stdout ---",
				stdout,
				"--- stderr ---",
				stderr,
				"",
			]
		)

	compose_file = output_root / "docker-compose.test.yml"
	if skip_compose_config:
		checks.append(
			{
				"name": "docker_compose_config",
				"status": "skipped",
				"reason": "skip_compose_config=true",
			}
		)
		log_sections.append("check: docker_compose_config\nstatus: skipped\nreason: skip_compose_config=true\n")
	elif compose_file.exists():
		command = ["docker", "compose", "-f", str(compose_file), "config"]
		exit_code, stdout, stderr, duration = _run_sanity_check(command, repo_root)
		status = "pass" if exit_code == 0 else "fail"
		if status == "fail":
			overall_status = "fail"
		checks.append(
			{
				"name": "docker_compose_config",
				"status": status,
				"exit_code": exit_code,
				"duration_seconds": round(duration, 3),
			}
		)
		log_sections.extend(
			[
				"check: docker_compose_config",
				f"command: {shlex.join(command)}",
				f"exit_code: {exit_code}",
				f"duration_seconds: {duration:.3f}",
				"--- stdout ---",
				stdout,
				"--- stderr ---",
				stderr,
				"",
			]
		)
	else:
		checks.append(
			{
				"name": "docker_compose_config",
				"status": "skipped",
				"reason": "no docker-compose.test.yml in output",
			}
		)
		log_sections.append("check: docker_compose_config\nstatus: skipped\nreason: no docker-compose.test.yml in output\n")

	stage_duration = time.monotonic() - stage_started
	log_path.parent.mkdir(parents=True, exist_ok=True)
	log_path.write_text("\n".join(log_sections), encoding="utf-8")
	return {
		"status": overall_status,
		"duration_seconds": round(stage_duration, 3),
		"checks": checks,
		"log_path": _repo_rel(log_path, repo_root),
	}


def _run_fixture(repo_root: Path, manifest_path: Path, logs_root: Path, skip_compose_config: bool) -> dict[str, Any]:
	fixture_name = manifest_path.name
	fixture_log_dir = logs_root / fixture_name
	output_root = fixture_log_dir / "rendered"
	if output_root.exists():
		shutil.rmtree(output_root)
	output_root.mkdir(parents=True, exist_ok=True)

	fixture_started = time.monotonic()
	stages: dict[str, dict[str, Any]] = {}

	render = _stage_render(repo_root, manifest_path, output_root, fixture_log_dir)
	stages["render"] = render
	if render["status"] == "pass":
		lint = _stage_lint(repo_root, output_root, fixture_log_dir)
		stages["lint"] = lint
	else:
		stages["lint"] = _skipped_stage("render_failed")

	if stages["render"]["status"] == "pass" and stages["lint"]["status"] == "pass":
		stages["sanity"] = _stage_sanity(repo_root, output_root, fixture_log_dir, skip_compose_config)
	else:
		stages["sanity"] = _skipped_stage("prior_stage_failed")

	fixture_status = "pass"
	for stage_name in ("render", "lint", "sanity"):
		if stages[stage_name]["status"] == "fail":
			fixture_status = "fail"
			break

	fixture_duration = time.monotonic() - fixture_started
	log_paths = {
		stage_name: stage_payload["log_path"]
		for stage_name, stage_payload in stages.items()
		if "log_path" in stage_payload
	}

	return {
		"name": fixture_name,
		"manifest_path": _repo_rel(manifest_path, repo_root),
		"status": fixture_status,
		"duration_seconds": round(fixture_duration, 3),
		"stages": stages,
		"log_paths": log_paths,
		"output_root": _repo_rel(output_root, repo_root),
	}


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
			"error": f"No fixture manifests found in {fixtures_root.as_posix()}",
		}
		summary_path.parent.mkdir(parents=True, exist_ok=True)
		summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
		print(summary["error"], file=sys.stderr)
		return 1

	for manifest_path in fixtures:
		fixture_results.append(
			_run_fixture(
				repo_root=repo_root,
				manifest_path=manifest_path,
				logs_root=logs_root,
				skip_compose_config=args.skip_compose_config,
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
