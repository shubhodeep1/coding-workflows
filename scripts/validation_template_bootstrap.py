#!/usr/bin/env python3
"""Shared onboarding helper for validation template manifests."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


STUB_MANIFEST_SOURCE = Path("examples/validation-fixtures/python-repo-checks.yml")
STUB_ENTRY_SCRIPT_SOURCE = Path("examples/validation-fixtures/run_validation_repo_checks.sh")
STUB_ENTRY_SCRIPT_RELATIVE = Path("scripts/run_validation_repo_checks.sh")


@dataclass(frozen=True)
class ValidationBootstrapResult:
	manifest_path: Path
	bootstrapped: bool
	diagnostics: tuple[str, ...]


def bootstrap_validation_manifest(*, source_root: Path, workspace_root: Path) -> ValidationBootstrapResult:
	"""Seed a documented placeholder manifest+entry script when absent."""

	manifest_path = workspace_root / ".ai" / "validate.yml"
	if manifest_path.is_file():
		return ValidationBootstrapResult(
			manifest_path=manifest_path,
			bootstrapped=False,
			diagnostics=(),
		)

	manifest_source = source_root / STUB_MANIFEST_SOURCE
	entry_script_source = source_root / STUB_ENTRY_SCRIPT_SOURCE
	if not manifest_source.is_file():
		raise FileNotFoundError(f"Validation stub manifest missing: {manifest_source.as_posix()}")
	if not entry_script_source.is_file():
		raise FileNotFoundError(f"Validation stub entry script missing: {entry_script_source.as_posix()}")

	manifest_path.parent.mkdir(parents=True, exist_ok=True)
	shutil.copy2(manifest_source, manifest_path)

	entry_script_path = workspace_root / STUB_ENTRY_SCRIPT_RELATIVE
	diagnostics = [f"manifest_bootstrapped_from: {STUB_MANIFEST_SOURCE.as_posix()}"]
	if not entry_script_path.exists():
		entry_script_path.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(entry_script_source, entry_script_path)
		diagnostics.append(f"repo_check_entry_seeded: {STUB_ENTRY_SCRIPT_RELATIVE.as_posix()}")
	else:
		diagnostics.append(f"repo_check_entry_preserved_existing: {STUB_ENTRY_SCRIPT_RELATIVE.as_posix()}")
	entry_script_path.chmod(entry_script_path.stat().st_mode | 0o111)

	return ValidationBootstrapResult(
		manifest_path=manifest_path,
		bootstrapped=True,
		diagnostics=tuple(diagnostics),
	)
