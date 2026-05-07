from pathlib import Path

from scripts.targeted_file_context import emit_context, extract_target_paths


def test_extracts_files_likely_to_change_section_only():
    plan = """
Implementation Plan

1. Files likely to change
- `contracts/FunOFTAdapter.sol`
- `test/CrossChain.test.ts`

2. Functions or modules to implement
- In `docs/not-a-target.md`, explain nothing.
"""

    assert extract_target_paths(plan) == [
        "contracts/FunOFTAdapter.sol",
        "test/CrossChain.test.ts",
    ]


def test_emits_line_numbered_bounded_context(tmp_path: Path):
    (tmp_path / "contracts").mkdir()
    target = tmp_path / "contracts" / "FunOFT.sol"
    target.write_text("line one\nline two\n", encoding="utf-8")

    context = emit_context(["contracts/FunOFT.sol"], tmp_path, max_files=10, max_bytes=1024)

    assert "=== TARGETED FILE CONTEXT ===" in context
    assert "--- FILE: contracts/FunOFT.sol" in context
    assert "1\tline one" in context
    assert "2\tline two" in context
    assert "Included 1 target file(s)" in context


def test_ignores_paths_outside_repo(tmp_path: Path):
    context = emit_context(["../secret.txt"], tmp_path, max_files=10, max_bytes=1024)

    assert "no existing target files could be inlined" in context
