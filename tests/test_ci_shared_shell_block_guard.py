#!/usr/bin/env python3
"""Contract tests for the early shared shell-block CI guard."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GUARD_STEP_NAME = "Shared shell-block anti-regression checks"

INLINE_CODEX_SCANNED = ",".join(
	(
		".github/workflows/clarify.yml",
		".github/workflows/plan.yml",
		".github/workflows/implement.yml",
		".github/workflows/review_autofix.yml",
		"scripts/validate_process.sh",
	)
)
REQUIRED_CODEX_SCANNED = ",".join(
	(
		".github/workflows/clarify.yml",
		".github/workflows/plan.yml",
		".github/workflows/implement.yml",
		"scripts/validate_process.sh",
	)
)
MEMORY_SCANNED = ",".join(
	(
		".github/workflows/clarify.yml",
		".github/workflows/plan.yml",
		".github/workflows/implement.yml",
		".github/workflows/validate.yml",
		".github/workflows/review_autofix.yml",
	)
)
TELEGRAM_SCANNED = ",".join(
	(
		".github/workflows/clarify.yml",
		".github/workflows/plan.yml",
		".github/workflows/implement.yml",
		".github/workflows/review_autofix.yml",
	)
)
WATCHDOG_SCANNED = ",".join(
	(
		".github/workflows/implement.yml",
		"scripts/review_apply_fixes.sh",
		"scripts/validate_process.sh",
	)
)


def _lint_steps() -> list[dict]:
	workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
	return workflow["jobs"]["lint"]["steps"]


def _guard_body() -> str:
	matching_steps = [step for step in _lint_steps() if step.get("name") == GUARD_STEP_NAME]
	if len(matching_steps) != 1:
		raise AssertionError(f"Expected exactly one {GUARD_STEP_NAME!r} step")
	return matching_steps[0]["run"]


def _write_valid_fixture(fixture_root: Path) -> None:
	fixture_contents = {
		".github/workflows/clarify.yml": (
			"codex_config_assemble\nmemory_bootstrap\ntg_send_phase_failure\n"
		),
		".github/workflows/plan.yml": (
			"codex_config_assemble\nmemory_bootstrap\ntg_send_phase_failure\n"
		),
		".github/workflows/implement.yml": (
			"codex_config_assemble\nmemory_bootstrap\ntg_send_phase_failure\n"
			"source scripts/watchdog_helpers.sh\n"
		),
		".github/workflows/review_autofix.yml": (
			"memory_bootstrap\ntg_send_phase_failure\n"
		),
		".github/workflows/validate.yml": "memory_bootstrap\n",
		"scripts/validate_process.sh": (
			"codex_config_assemble\nsource scripts/watchdog_helpers.sh\n"
		),
		"scripts/review_apply_fixes.sh": "source scripts/watchdog_helpers.sh\n",
	}
	for relative_path, contents in fixture_contents.items():
		target_path = fixture_root / relative_path
		target_path.parent.mkdir(parents=True, exist_ok=True)
		target_path.write_text(contents, encoding="utf-8")


def _replace_once(fixture_root: Path, relative_path: str, old: str, new: str) -> None:
	target_path = fixture_root / relative_path
	contents = target_path.read_text(encoding="utf-8")
	if contents.count(old) != 1:
		raise AssertionError(f"Expected one {old!r} occurrence in {relative_path}")
	target_path.write_text(contents.replace(old, new, 1), encoding="utf-8")


def _append(fixture_root: Path, relative_path: str, content: str) -> None:
	target_path = fixture_root / relative_path
	with target_path.open("a", encoding="utf-8") as fixture_file:
		fixture_file.write(content)


def _run_guard(fixture_root: Path) -> subprocess.CompletedProcess[str]:
	fixture_environment = os.environ.copy()
	fixture_environment.pop("BASH_ENV", None)
	return subprocess.run(
		["bash", "-c", _guard_body()],
		cwd=fixture_root,
		env=fixture_environment,
		text=True,
		capture_output=True,
		check=False,
	)


class GuardOrderingContractTest(unittest.TestCase):
	def test_guard_immediately_follows_checkout_and_precedes_setup_install_lint_and_tests(self) -> None:
		step_names = [step.get("name") for step in _lint_steps()]
		checkout_index = step_names.index("Checkout repository")
		guard_index = step_names.index(GUARD_STEP_NAME)
		self.assertEqual(guard_index, checkout_index + 1)
		for later_step in (
			"Setup Python",
			"Install Python CI dependencies",
			"YAML lint",
			"Orchestrate poll implementation-failed regression fast-fail",
		):
			self.assertLess(guard_index, step_names.index(later_step))

	def test_guard_contract_test_is_wired_into_ci(self) -> None:
		workflow_text = CI_WORKFLOW.read_text(encoding="utf-8")
		self.assertIn(
			"PYTHONDONTWRITEBYTECODE=1 python3 tests/test_ci_shared_shell_block_guard.py",
			workflow_text,
		)


class GuardBehaviorContractTest(unittest.TestCase):
	def test_valid_shared_helper_wiring_passes(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			fixture_root = Path(temporary_directory)
			_write_valid_fixture(fixture_root)
			result = _run_guard(fixture_root)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertNotIn("CI_GUARD_FAILURE", result.stdout + result.stderr)

	def test_each_existing_rejection_emits_actionable_diagnostic_fields(self) -> None:
		cases = (
			(
				"inline codex config",
				lambda root: _append(
					root,
					".github/workflows/clarify.yml",
					"WRITE_CODEX_CONFIG=do-not-echo-secret\n",
				),
				"codex-config-shared-helper",
				"forbidden-inline-config-assembly",
				".github/workflows/clarify.yml",
				"scripts/codex_helpers.sh:codex_config_assemble",
				INLINE_CODEX_SCANNED,
				True,
			),
			(
				"missing codex helper",
				lambda root: _replace_once(
					root, ".github/workflows/plan.yml", "codex_config_assemble\n", ""
				),
				"codex-config-shared-helper",
				"required-helper-missing",
				".github/workflows/plan.yml",
				"codex_config_assemble",
				REQUIRED_CODEX_SCANNED,
				False,
			),
			(
				"codex helper in OpenCode review",
				lambda root: _append(
					root, ".github/workflows/review_autofix.yml", "codex_config_assemble\n"
				),
				"opencode-only-review",
				"forbidden-codex-config-assembly",
				".github/workflows/review_autofix.yml",
				"OpenCode-only-review-without-codex_config_assemble",
				".github/workflows/review_autofix.yml",
				True,
			),
			(
				"missing memory helper",
				lambda root: _replace_once(
					root, ".github/workflows/validate.yml", "memory_bootstrap\n", ""
				),
				"memory-bootstrap-shared-helper",
				"required-helper-missing",
				".github/workflows/validate.yml",
				"memory_bootstrap",
				MEMORY_SCANNED,
				False,
			),
			(
				"missing Telegram helper",
				lambda root: _replace_once(
					root, ".github/workflows/implement.yml", "tg_send_phase_failure\n", ""
				),
				"telegram-failure-shared-helper",
				"required-helper-missing",
				".github/workflows/implement.yml",
				"tg_send_phase_failure",
				TELEGRAM_SCANNED,
				False,
			),
			(
				"inline watchdog parser",
				lambda root: _append(
					root, "scripts/review_apply_fixes.sh", "_reap_editor_fifo_holders()\n"
				),
				"watchdog-shared-helper",
				"forbidden-inline-stall-parser",
				"scripts/review_apply_fixes.sh",
				"scripts/watchdog_helpers.sh",
				WATCHDOG_SCANNED,
				True,
			),
			(
				"missing watchdog helper",
				lambda root: _replace_once(
					root,
					"scripts/validate_process.sh",
					"source scripts/watchdog_helpers.sh\n",
					"",
				),
				"watchdog-shared-helper",
				"required-helper-missing",
				"scripts/validate_process.sh",
				"watchdog_helpers.sh",
				WATCHDOG_SCANNED,
				False,
			),
		)

		for (
			case_name,
			mutate_fixture,
			expected_guard,
			expected_check,
			expected_file,
			expected_policy,
			expected_scanned_files,
			expects_line_number,
		) in cases:
			with self.subTest(case_name=case_name):
				with tempfile.TemporaryDirectory() as temporary_directory:
					fixture_root = Path(temporary_directory)
					_write_valid_fixture(fixture_root)
					mutate_fixture(fixture_root)
					result = _run_guard(fixture_root)
				output = result.stdout + result.stderr
				self.assertNotEqual(result.returncode, 0, output)
				self.assertIn("CI_GUARD_FAILURE", output)
				self.assertIn(f"guard={expected_guard}", output)
				self.assertIn(f"check={expected_check}", output)
				self.assertIn(f"file={expected_file}", output)
				self.assertIn(f"expected={expected_policy}", output)
				self.assertIn(f"scanned_files={expected_scanned_files}", output)
				if expects_line_number:
					self.assertRegex(output, r"line=\d+")
				else:
					self.assertIn("line=none", output)

	def test_forbidden_match_output_omits_source_contents_and_reports_every_match(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			fixture_root = Path(temporary_directory)
			_write_valid_fixture(fixture_root)
			_append(
				fixture_root,
				".github/workflows/clarify.yml",
				"WRITE_CODEX_CONFIG=first-sensitive-value\n"
				"WRITE_CODEX_CONFIG=second-sensitive-value\n",
			)
			result = _run_guard(fixture_root)
		output = result.stdout + result.stderr
		self.assertNotEqual(result.returncode, 0, output)
		self.assertEqual(output.count("CI_GUARD_FAILURE"), 2)
		self.assertEqual(len(re.findall(r"line=\d+", output)), 2)
		self.assertNotIn("first-sensitive-value", output)
		self.assertNotIn("second-sensitive-value", output)


if __name__ == "__main__":
	unittest.main()
