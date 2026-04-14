#!/usr/bin/env python3
"""Regression tests for validate_process startup guard scope."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS_SCRIPT = REPO_ROOT / "scripts" / "validate_process.sh"


def _extract_function(script_text: str, function_name: str) -> str:
    pattern = re.compile(
        rf"^{re.escape(function_name)}\(\)\n\{{\n.*?^\}}\n",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(script_text)
    if match is None:
        raise AssertionError(f"Could not find function {function_name} in validate_process.sh")
    return match.group(0)


def _run_guard(repo_dir: Path, function_blocks: str, call: str) -> subprocess.CompletedProcess[str]:
    harness = (
        "set -euo pipefail\n"
        f"{function_blocks}\n"
        "set +e\n"
        f"{call}\n"
        "rc=$?\n"
        "set -e\n"
        "exit \"${rc}\"\n"
    )
    return subprocess.run(
        ["bash"],
        input=harness,
        text=True,
        capture_output=True,
        cwd=repo_dir,
        check=False,
    )


def _init_repo(repo_dir: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "test-bot"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test-bot@example.com"], cwd=repo_dir, check=True)


def _write_file(path: Path, content: str = "#!/usr/bin/env bash\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit_all(repo_dir: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "test"], cwd=repo_dir, check=True, stdout=subprocess.DEVNULL)


class TestValidateProcessDriverGuardScope(unittest.TestCase):

    def test_unrelated_validate_helper_script_is_allowed(self) -> None:
        script_text = VALIDATE_PROCESS_SCRIPT.read_text(encoding="utf-8")
        function_blocks = "\n".join(
            [
                _extract_function(script_text, "ensure_validation_harness_not_tracked"),
                _extract_function(script_text, "enforce_managed_validation_artifact_contract"),
            ]
        )

        with tempfile.TemporaryDirectory(prefix="validate-guard-allow-") as td:
            repo_dir = Path(td)
            _init_repo(repo_dir)
            _write_file(repo_dir / "scripts/validate_process.sh")
            _write_file(repo_dir / "scripts/validate_local.sh")
            _commit_all(repo_dir)

            result = _run_guard(
                repo_dir,
                function_blocks,
                "ensure_validation_harness_not_tracked && enforce_managed_validation_artifact_contract",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Managed validation artifact must remain untracked:", result.stderr)
        self.assertNotIn("Found tracked copies of managed validation artifacts:", result.stderr)

    def test_tracked_validation_validate_sh_is_rejected(self) -> None:
        script_text = VALIDATE_PROCESS_SCRIPT.read_text(encoding="utf-8")
        function_block = _extract_function(script_text, "ensure_validation_harness_not_tracked")

        with tempfile.TemporaryDirectory(prefix="validate-guard-tracked-harness-") as td:
            repo_dir = Path(td)
            _init_repo(repo_dir)
            _write_file(repo_dir / "validation/validate.sh")
            _commit_all(repo_dir)

            result = _run_guard(repo_dir, function_block, "ensure_validation_harness_not_tracked")

        self.assertNotEqual(result.returncode, 0, "tracked validation/validate.sh must be rejected")
        self.assertIn("Managed validation artifact must remain untracked: validation/validate.sh", result.stderr)

    def test_tracked_duplicate_managed_artifact_copy_is_rejected(self) -> None:
        script_text = VALIDATE_PROCESS_SCRIPT.read_text(encoding="utf-8")
        function_block = _extract_function(script_text, "enforce_managed_validation_artifact_contract")

        with tempfile.TemporaryDirectory(prefix="validate-guard-dup-") as td:
            repo_dir = Path(td)
            _init_repo(repo_dir)
            _write_file(repo_dir / "archive/validation/validate.sh")
            _commit_all(repo_dir)

            result = _run_guard(repo_dir, function_block, "enforce_managed_validation_artifact_contract")

        self.assertNotEqual(result.returncode, 0, "tracked duplicate managed artifact copies must be rejected")
        self.assertIn("Found tracked copies of managed validation artifacts:", result.stderr)
        self.assertIn("archive/validation/validate.sh", result.stderr)


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestValidateProcessDriverGuardScope)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
