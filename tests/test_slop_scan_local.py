#!/usr/bin/env python3
"""Tests for the local slop-scan helper."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "slop_scan"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import slop_scan_local


def _result_for_fixture(path: Path) -> dict:
	relative_path = path.relative_to(REPO_ROOT).as_posix()
	return slop_scan_local.collect_scan_result([relative_path], REPO_ROOT, restrict_scope=False)


def test_empty_catch_around_os_unlink_fixture_emits_expected_finding() -> None:
	result = _result_for_fixture(FIXTURES_DIR / "empty_catch_around_os_unlink.py")

	assert result["collection_status"] == "ok"
	assert result["suppressed_findings"] == []
	assert any(finding["rule_id"] == "empty_catch_file_op" for finding in result["findings"])


def test_safe_unlink_quiet_cleanup_fixture_is_suppressed() -> None:
	result = _result_for_fixture(FIXTURES_DIR / "safe_unlink_quiet_cleanup.py")

	assert result["collection_status"] == "ok"
	assert result["findings"] == []
	assert any(
		finding["rule_id"] == "empty_catch_file_op"
		and finding.get("not_to_fix_reason") == "best_effort_cleanup_helper"
		for finding in result["suppressed_findings"]
	)


def test_python3_heredoc_findings_map_back_to_shell_line_numbers(tmp_path: Path) -> None:
	shell_file = tmp_path / "scripts" / "example.sh"
	shell_file.parent.mkdir(parents=True, exist_ok=True)
	shell_file.write_text(
		"#!/usr/bin/env bash\n"
		"python3 - <<'PY'\n"
		"def remove_temp(path):\n"
		"\ttry:\n"
		"\t\timport os\n"
		"\t\tos.unlink(path)\n"
		"\texcept:\n"
		"\t\tpass\n"
		"PY\n",
		encoding="utf-8",
	)

	result = slop_scan_local.collect_scan_result(["scripts/example.sh"], tmp_path)

	finding = next(finding for finding in result["findings"] if finding["rule_id"] == "empty_catch_file_op")
	assert finding["path"] == "scripts/example.sh"
	assert finding["line"] == 7
	assert finding["source_kind"] == "python_heredoc"
