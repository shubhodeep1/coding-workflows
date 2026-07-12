#!/usr/bin/env python3
"""Tests for scripts/lint_plan_decisions.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "lint_plan_decisions.py"


def _import_lint_module():
	spec = importlib.util.spec_from_file_location("lint_plan_decisions", SCRIPT_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules["lint_plan_decisions"] = module
	spec.loader.exec_module(module)
	return module


def _write_plan(temp_root: Path, relative_name: str, markdown_text: str) -> Path:
	plan_directory = temp_root / "docs" / "plans"
	plan_directory.mkdir(parents=True, exist_ok=True)
	plan_path = plan_directory / relative_name
	plan_path.write_text(markdown_text, encoding="utf-8")
	return plan_path


def test_complete_decision_record_emits_no_warning() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(
			temp_root,
			"valid-plan.md",
			"""# Plan\n\n## Decisions\n\n### D1 — Keep the helper advisory\n\n- **Chosen:** add a warning-only linter.\n- **Alternatives considered:**\n  - **Fail closed** — rejected for rollout safety.\n- **Why:** current plans are legacy-heavy and need a bake-out window.\n\n## Risks and edge cases\n\n- Minimal.\n""",
		)

		warnings = mod.lint_file(plan_path)
		assert warnings == []


def test_missing_plan_directory_emits_no_warning() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)

		assert mod.discover_plan_files(temp_root) == []
		assert mod.lint_tree(temp_root) == []


def test_missing_decisions_section_warns() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(temp_root, "missing-decisions.md", "# Plan\n\n## Risks\n\n- None.\n")

		warnings = mod.lint_file(plan_path)
		assert warnings == ["missing `## Decisions` section"]


def test_read_error_emits_file_specific_warning() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(temp_root, "unreadable.md", "# Plan\n")

		with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
			warnings = mod.lint_file(plan_path)

		assert warnings == ["could not read plan file (PermissionError: denied)"]


def test_decisions_section_without_valid_records_warns() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(
			temp_root,
			"no-records.md",
			"""# Plan\n\n## Decisions\n\nIntro text only.\n\n## Risks\n\n- None.\n""",
		)

		warnings = mod.lint_file(plan_path)
		assert warnings == ["has `## Decisions` but no `### D<n> — <title>` decision records"]


def test_markdown_field_variants_count_as_present() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(
			temp_root,
			"field-variants.md",
			"""# Plan\n\n## Decisions\n\n### D2 — Accept common markdown variants\n\n* **Chosen**: allow asterisk bullets and colon-outside-bold markers.\n+ **Alternatives considered:**\n  - Keep only the strict variant.\n- **Why** allow the dash bullet when the colon is omitted.\n""",
		)

		warnings = mod.lint_file(plan_path)
		assert warnings == []


def test_malformed_decision_heading_warns() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(
			temp_root,
			"malformed-heading.md",
			"""# Plan\n\n## Decisions\n\n### Decision one\n\n- **Chosen:** keep the old heading.\n- **Alternatives considered:** none.\n- **Why:** this should warn.\n""",
		)

		warnings = mod.lint_file(plan_path)
		assert warnings == [
			"decision heading `### Decision one` does not match required shape `### D<n> — <title>`"
		]


def test_missing_required_decision_fields_are_named() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(
			temp_root,
			"missing-fields.md",
			"""# Plan\n\n## Decisions\n\n### D7 — Leave legacy plans untouched\n\n- **Chosen:** avoid backfilling legacy docs in this phase.\n""",
		)

		warnings = mod.lint_file(plan_path)
		assert warnings == [
			"D7 — Leave legacy plans untouched is missing required bullet(s): `Alternatives considered`, `Why`"
		]


def test_field_without_inline_or_following_content_warns() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(
			temp_root,
			"empty-field.md",
			"""# Plan\n\n## Decisions\n\n### D8 — Reject empty field markers\n\n- **Chosen:**\n- **Alternatives considered:** compare alternatives here.\n- **Why:** explain the final choice here.\n""",
		)

		warnings = mod.lint_file(plan_path)
		assert warnings == ["D8 — Reject empty field markers is missing required bullet(s): `Chosen`"]


def test_bare_list_markers_do_not_count_as_field_content() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(
			temp_root,
			"bare-markers.md",
			"""# Plan\n\n## Decisions\n\n### D10 — Reject bare marker placeholders\n\n- **Chosen:** -\n- **Alternatives considered:**\n  +\n- **Why:** explain the final choice here.\n""",
		)

		warnings = mod.lint_file(plan_path)
		assert warnings == [
			"D10 — Reject bare marker placeholders is missing required bullet(s): `Chosen`, `Alternatives considered`"
		]


def test_terminal_empty_field_warns() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(
			temp_root,
			"terminal-empty-field.md",
			"""# Plan\n\n## Decisions\n\n### D11 — Reject terminal empty fields\n\n- **Chosen:** keep the linter advisory-only.\n- **Alternatives considered:** fail closed in CI.\n- **Why:**\n""",
		)

		warnings = mod.lint_file(plan_path)
		assert warnings == ["D11 — Reject terminal empty fields is missing required bullet(s): `Why`"]


def test_multiple_malformed_records_emit_multiple_warnings() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(
			temp_root,
			"multiple-warnings.md",
			"""# Plan\n\n## Decisions\n\n### D1 — First record\n\n- **Chosen:** keep it additive.\n- **Why:** one field is missing on purpose.\n\n### Wrong heading\n\n- **Chosen:** malformed title shape.\n- **Alternatives considered:** none.\n- **Why:** should warn too.\n""",
		)

		warnings = mod.lint_file(plan_path)
		assert warnings == [
			"D1 — First record is missing required bullet(s): `Alternatives considered`",
			"decision heading `### Wrong heading` does not match required shape `### D<n> — <title>`",
		]


def test_fenced_code_block_heading_lines_do_not_truncate_decisions_section() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		plan_path = _write_plan(
			temp_root,
			"fenced-code.md",
			"""# Plan\n\n## Decisions\n\n```md\n# Example heading inside code\n## Example subheading inside code\n```\n\n### D9 — Keep parsing after fenced code blocks\n\n- **Chosen:** continue scanning after code fences.\n- **Alternatives considered:** stop scanning at heading-like code lines.\n- **Why:** fenced code is content, not structure.\n\n## Risks\n\n- Minimal.\n""",
		)

		warnings = mod.lint_file(plan_path)
		assert warnings == []


def test_main_emits_structured_warnings_and_returns_zero() -> None:
	mod = _import_lint_module()
	with tempfile.TemporaryDirectory() as temp_dir_name:
		temp_root = Path(temp_dir_name)
		_write_plan(temp_root, "warning-plan.md", "# Plan\n")

		stderr_buffer = io.StringIO()
		with contextlib.redirect_stderr(stderr_buffer):
			exit_code = mod.main(["--root", str(temp_root)])

		stderr_text = stderr_buffer.getvalue()
		assert exit_code == 0
		assert "::warning file=" in stderr_text
		assert "[lint_plan_decisions] missing `## Decisions` section" in stderr_text
		assert "warning-plan.md" in stderr_text


def test_main_returns_zero_when_linting_raises_unexpected_exception() -> None:
	mod = _import_lint_module()
	stderr_buffer = io.StringIO()

	with mock.patch.object(mod, "lint_tree", side_effect=PermissionError("denied")):
		with contextlib.redirect_stderr(stderr_buffer):
			exit_code = mod.main([])

	stderr_text = stderr_buffer.getvalue()
	assert exit_code == 0
	assert "[lint_plan_decisions] unexpected linter failure (PermissionError: denied); continuing fail-open" in stderr_text


def test_main_returns_zero_on_argument_parse_failure() -> None:
	mod = _import_lint_module()
	stderr_buffer = io.StringIO()

	with contextlib.redirect_stderr(stderr_buffer):
		exit_code = mod.main(["--not-a-real-flag"])

	stderr_text = stderr_buffer.getvalue()
	assert exit_code == 0
	assert "[lint_plan_decisions] argument parsing failed with exit=2; continuing fail-open" in stderr_text


def main() -> int:
	try:
		import sys as _sys
		_sys.stdout.reconfigure(line_buffering=True)
	except Exception:
		pass
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	passed = 0
	failed = 0
	for test_function in test_functions:
		test_name = test_function.__name__
		try:
			test_function()
			print(f"  PASS  {test_name}", flush=True)
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {test_name}: {exc}", flush=True)
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
