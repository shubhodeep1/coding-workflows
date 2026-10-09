#!/usr/bin/env python3
"""Exercise the size and safety contract for workflow-log include staging."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "stage_workflow_log_bundle.py"


def invoke(src: Path, stage: Path, limit: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), str(src), str(stage), limit],
		capture_output=True, text=True, check=False)


def test_full_bundle_requires_no_copy(tmp_path: Path) -> None:
	src = tmp_path / "workflow-log-output"
	src.mkdir()
	(src / "summary.json").write_text("{}", encoding="utf-8")
	stage = tmp_path / "bounded"
	result = invoke(src, stage, "1024")
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == {"mode": "full", "path": str(src), "bytes": 2,
		"total_bytes": 2, "omitted_runs": 0}
	assert not stage.exists()


def test_bounded_prioritizes_complete_error_runs_and_lists_omissions(tmp_path: Path) -> None:
	src = tmp_path / "workflow-log-output"
	src.mkdir()
	(src / "summary.json").write_text("{}", encoding="utf-8")
	for category, run_id in (("errors", "1"), ("errors", "2"), ("slow", "3"), ("recent", "4")):
		run = src / category / "repo" / "family" / run_id
		run.mkdir(parents=True)
		(run / "metadata.json").write_text("x" * 110, encoding="utf-8")
	outside = tmp_path / "outside"
	outside.write_text("not included", encoding="utf-8")
	(src / "errors" / "repo" / "family" / "1" / "escape.log").symlink_to(outside)
	stage = tmp_path / "bounded"
	result = invoke(src, stage, "400")
	assert result.returncode == 0, result.stderr
	info = json.loads(result.stdout)
	assert info["mode"] == "bounded" and info["path"] == str(stage)
	assert info["bytes"] <= 400 and info["total_bytes"] == 442
	assert (stage / "summary.json").read_text(encoding="utf-8") == "{}"
	for run_id in ("1", "2"):
		assert (stage / "errors" / "repo" / "family" / run_id / "metadata.json").is_file()
	assert not (stage / "slow" / "repo" / "family" / "3").exists()
	assert not (stage / "recent" / "repo" / "family" / "4").exists()
	assert not (stage / "errors" / "repo" / "family" / "1" / "escape.log").exists()
	note = (stage / "BOUNDED_SUBSET.txt").read_text(encoding="utf-8")
	assert "slow/repo/family/3" in note and "recent/repo/family/4" in note
	assert info["omitted_runs"] == 2
	assert sum(path.stat().st_size for path in stage.rglob("*") if path.is_file()) == info["bytes"]


def test_invalid_cap_and_existing_stage_fail_without_overwriting(tmp_path: Path) -> None:
	src = tmp_path / "workflow-log-output"
	src.mkdir()
	(src / "summary.json").write_bytes(b"x" * 300)
	stage = tmp_path / "bounded"
	assert invoke(src, stage, "0").returncode == 2
	assert invoke(src, stage, "invalid").returncode == 2
	stage.mkdir()
	assert invoke(src, stage, "200").returncode != 0
	assert stage.is_dir()


def test_symlinked_category_and_unsafe_names_are_not_staged(tmp_path: Path) -> None:
	src = tmp_path / "workflow-log-output"
	src.mkdir()
	(src / "summary.json").write_bytes(b"s" * 300)
	(src / "unsafe\nfile").write_text("x", encoding="utf-8")
	outside = tmp_path / "outside"
	(outside / "repo" / "family" / "5").mkdir(parents=True)
	(outside / "repo" / "family" / "5" / "metadata.json").write_text("secret", encoding="utf-8")
	(src / "errors").symlink_to(outside, target_is_directory=True)
	stage = tmp_path / "bounded"
	result = invoke(src, stage, "200")
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout)["mode"] == "bounded"
	assert not (stage / "errors").exists()
	assert not (stage / "unsafe\nfile").exists()


def test_failed_copy_removes_partial_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	spec = importlib.util.spec_from_file_location("stage_workflow_log_bundle", SCRIPT)
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	src = tmp_path / "workflow-log-output"
	src.mkdir()
	(src / "summary.json").write_bytes(b"s" * 100)
	(src / "other.log").write_bytes(b"x" * 300)
	stage = tmp_path / "bounded"

	def fail_copy(path: Path, destination: Path, size: int) -> None:
		destination.write_bytes(b"partial")
		raise OSError("simulated write failure")

	monkeypatch.setattr(module, "copy_file", fail_copy)
	with pytest.raises(OSError, match="simulated write failure"):
		module.stage_bundle(src, stage, 200)
	assert not stage.exists()


def test_include_ceiling_matches_isolation_helper() -> None:
	spec = importlib.util.spec_from_file_location("stage_workflow_log_bundle", SCRIPT)
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	isolation_source = (ROOT / "scripts" / "codex_isolated_workspace.py").read_text(encoding="utf-8")
	assert f"MAX_INCLUDE_TOTAL = {module.MAX_INCLUDE_TOTAL // (1024 * 1024)} * 1024 * 1024" in isolation_source
	assert module.INCLUDE_HEADROOM >= 1024 * 1024
