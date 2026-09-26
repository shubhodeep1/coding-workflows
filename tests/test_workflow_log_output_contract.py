#!/usr/bin/env python3

from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = REPO_ROOT / "scripts" / "workflow_log_output_contract.py"


def test_retro_output_is_canonicalized_and_published(tmp_path: Path) -> None:
	candidate = tmp_path / "candidate.md"
	output = tmp_path / "published.md"
	candidate.write_text(
		"## Weekly Retro\nsummary\n### What Worked\na\n### Failure Modes\nb\n"
		"### Next Week Recommendation\nc\n### Metrics Snapshot\nd\n\n",
		encoding="utf-8",
	)
	subprocess.run(
		["python3", str(VALIDATOR), "--mode", "retro", "--candidate", str(candidate), "--output", str(output), "--allowed-output-root", str(tmp_path)],
		check=True,
	)
	assert output.read_text(encoding="utf-8").endswith("d\n")


def test_output_with_control_bytes_is_rejected(tmp_path: Path) -> None:
	candidate = tmp_path / "candidate.md"
	candidate.write_bytes(b"## Weekly Retro\n\x1b[31m\n")
	result = subprocess.run(
		["python3", str(VALIDATOR), "--mode", "retro", "--candidate", str(candidate), "--output", str(tmp_path / "output.md")],
		check=False,
	)
	assert result.returncode != 0
	assert not (tmp_path / "output.md").exists()
