#!/usr/bin/env python3
"""Contract checks for memory_maintenance Codex no-op semantics."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WF_PATH = REPO_ROOT / ".github" / "workflows" / "memory_maintenance.yml"


def _workflow_text() -> str:
	return WF_PATH.read_text(encoding="utf-8")


def test_memory_maintenance_declares_codex_contract_noop() -> None:
	wf = _workflow_text()
	assert "batch_noop" in wf
	assert "codex_contract_noop" in wf
	assert "no_codex_execution_path" in wf


if __name__ == "__main__":
	test_memory_maintenance_declares_codex_contract_noop()
