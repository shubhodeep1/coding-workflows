#!/usr/bin/env python3
"""Unit tests for validation self-test status writer."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "validation_selftest_status.py"

spec = importlib.util.spec_from_file_location("validation_selftest_status", MODULE_PATH)
assert spec is not None and spec.loader is not None
status_writer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = status_writer
spec.loader.exec_module(status_writer)


def _write_json(path: Path, payload: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict:
	return json.loads(path.read_text(encoding="utf-8"))


def _summary(*, overall_status: str, generated_at: str, fixtures: int, passed: int, failed: int) -> dict:
	return {
		"schema_version": "1",
		"generated_at": generated_at,
		"overall_status": overall_status,
		"totals": {
			"fixtures": fixtures,
			"passed": passed,
			"failed": failed,
		},
	}


def test_first_green_run_from_seeded_status_starts_streak_at_one() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-status-") as tmp:
		tmp_path = Path(tmp)
		summary_path = tmp_path / "artifacts" / "validation-selftest-summary.json"
		status_path = tmp_path / "analysis" / "validation-selftest-status.json"
		_write_json(summary_path, _summary(overall_status="pass", generated_at="2026-04-25T00:00:00Z", fixtures=4, passed=4, failed=0))
		_write_json(
			status_path,
			{
				"schema_version": "1",
				"consecutive_green_runs": 0,
				"latest_run": {
					"generated_at": "unknown",
					"overall_status": "unknown",
					"summary_schema_version": "unknown",
					"totals": {"fixtures": 0, "passed": 0, "failed": 0},
				},
			},
		)

		exit_code = status_writer.main(["--summary-path", str(summary_path), "--status-path", str(status_path)])
		assert exit_code == 0

		status = _read_json(status_path)
		assert status["schema_version"] == "1"
		assert status["consecutive_green_runs"] == 1
		assert status["latest_run"]["overall_status"] == "pass"
		assert status["latest_run"]["generated_at"] == "2026-04-25T00:00:00Z"
		assert status["latest_run"]["totals"] == {"fixtures": 4, "passed": 4, "failed": 0}


def test_green_after_green_increments_streak() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-status-") as tmp:
		tmp_path = Path(tmp)
		summary_path = tmp_path / "artifacts" / "validation-selftest-summary.json"
		status_path = tmp_path / "analysis" / "validation-selftest-status.json"
		_write_json(summary_path, _summary(overall_status="pass", generated_at="2026-04-26T00:00:00Z", fixtures=4, passed=4, failed=0))
		_write_json(
			status_path,
			{
				"schema_version": "1",
				"consecutive_green_runs": 3,
				"latest_run": {
					"generated_at": "2026-04-25T00:00:00Z",
					"overall_status": "pass",
					"summary_schema_version": "1",
					"totals": {"fixtures": 4, "passed": 4, "failed": 0},
				},
			},
		)

		exit_code = status_writer.main(["--summary-path", str(summary_path), "--status-path", str(status_path)])
		assert exit_code == 0

		status = _read_json(status_path)
		assert status["consecutive_green_runs"] == 4
		assert status["latest_run"]["generated_at"] == "2026-04-26T00:00:00Z"


def test_failure_resets_streak_to_zero() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-status-") as tmp:
		tmp_path = Path(tmp)
		summary_path = tmp_path / "artifacts" / "validation-selftest-summary.json"
		status_path = tmp_path / "analysis" / "validation-selftest-status.json"
		_write_json(summary_path, _summary(overall_status="fail", generated_at="2026-04-27T00:00:00Z", fixtures=4, passed=2, failed=2))
		_write_json(
			status_path,
			{
				"schema_version": "1",
				"consecutive_green_runs": 5,
				"latest_run": {
					"generated_at": "2026-04-26T00:00:00Z",
					"overall_status": "pass",
					"summary_schema_version": "1",
					"totals": {"fixtures": 4, "passed": 4, "failed": 0},
				},
			},
		)

		exit_code = status_writer.main(["--summary-path", str(summary_path), "--status-path", str(status_path)])
		assert exit_code == 0

		status = _read_json(status_path)
		assert status["consecutive_green_runs"] == 0
		assert status["latest_run"]["overall_status"] == "fail"


def test_identical_summary_keeps_streak_and_serialization_stable() -> None:
	with tempfile.TemporaryDirectory(prefix="validation-selftest-status-") as tmp:
		tmp_path = Path(tmp)
		summary_path = tmp_path / "artifacts" / "validation-selftest-summary.json"
		status_path = tmp_path / "analysis" / "validation-selftest-status.json"
		summary = _summary(overall_status="pass", generated_at="2026-04-28T00:00:00Z", fixtures=4, passed=4, failed=0)
		_write_json(summary_path, summary)
		_write_json(
			status_path,
			{
				"schema_version": "1",
				"consecutive_green_runs": 7,
				"latest_run": {
					"generated_at": "2026-04-28T00:00:00Z",
					"overall_status": "pass",
					"summary_schema_version": "1",
					"totals": {"fixtures": 4, "passed": 4, "failed": 0},
				},
			},
		)

		first_exit = status_writer.main(["--summary-path", str(summary_path), "--status-path", str(status_path)])
		assert first_exit == 0
		first_contents = status_path.read_text(encoding="utf-8")
		first_status = _read_json(status_path)
		assert first_status["consecutive_green_runs"] == 7

		second_exit = status_writer.main(["--summary-path", str(summary_path), "--status-path", str(status_path)])
		assert second_exit == 0
		second_contents = status_path.read_text(encoding="utf-8")
		assert second_contents == first_contents


def main() -> int:
	test_first_green_run_from_seeded_status_starts_streak_at_one()
	test_green_after_green_increments_streak()
	test_failure_resets_streak_to_zero()
	test_identical_summary_keeps_streak_and_serialization_stable()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
