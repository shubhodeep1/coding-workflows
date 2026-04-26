#!/usr/bin/env python3
"""Contract coverage for canonical audit-gate template atomic delivery."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
APPLY_SCRIPT = REPO_ROOT / "scripts" / "apply_audit_gate_assets.py"
CANONICAL_CONTRACT_ROOT = REPO_ROOT / "workflow-templates" / "audit-gate"


def _write_json(path: Path, payload: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _run_apply(*, contract_root: Path, repo_root: Path) -> subprocess.CompletedProcess[str]:
	result_file = repo_root / ".tmp" / "audit_gate_result.json"
	changed_files_file = repo_root / ".tmp" / "audit_gate_changed_files.txt"
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		[
			"python3",
			str(APPLY_SCRIPT),
			"--contract-root",
			str(contract_root),
			"--repo-root",
			str(repo_root),
			"--result-file",
			str(result_file),
			"--changed-files-file",
			str(changed_files_file),
		],
		cwd=str(repo_root),
		text=True,
		capture_output=True,
		check=False,
		env=env,
	)


def _read_json(path: Path) -> dict:
	return json.loads(path.read_text(encoding="utf-8"))


def _read_changed_files(path: Path) -> list[str]:
	if not path.is_file():
		return []
	return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _build_npm_wrapper_env(*, repo_root: Path, audit_payload: dict) -> dict[str, str]:
	real_npm = shutil.which("npm")
	assert real_npm is not None, "npm is required for audit gate probe"

	payload_path = repo_root / ".tmp" / "npm_audit_payload.json"
	_write_json(payload_path, audit_payload)

	wrapper_dir = repo_root / ".tmp" / "bin"
	wrapper_dir.mkdir(parents=True, exist_ok=True)
	wrapper_path = wrapper_dir / "npm"
	wrapper_path.write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n"
		"if [ \"${1:-}\" = \"audit\" ] && [ \"${2:-}\" = \"--json\" ]; then\n"
		"\tcat \"${AUDIT_GATE_FAKE_AUDIT_JSON}\"\n"
		"\texit \"${AUDIT_GATE_FAKE_AUDIT_EXIT_CODE:-0}\"\n"
		"fi\n"
		"exec \"${AUDIT_GATE_REAL_NPM}\" \"$@\"\n",
		encoding="utf-8",
	)
	wrapper_path.chmod(0o755)

	env = os.environ.copy()
	env["PATH"] = f"{wrapper_dir}:{env.get('PATH', '')}"
	env["AUDIT_GATE_REAL_NPM"] = real_npm
	env["AUDIT_GATE_FAKE_AUDIT_JSON"] = str(payload_path)
	env["AUDIT_GATE_FAKE_AUDIT_EXIT_CODE"] = "0"
	return env


def test_fresh_apply_vendors_script_and_package_script_atomically() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-gate-atomic-") as td:
		repo_root = Path(td)
		_write_json(
			repo_root / "package.json",
			{
				"name": "fixture",
				"version": "1.0.0",
				"scripts": {
					"test": "echo ok"
				},
			},
		)

		result = _run_apply(contract_root=CANONICAL_CONTRACT_ROOT, repo_root=repo_root)
		assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"

		result_payload = _read_json(repo_root / ".tmp" / "audit_gate_result.json")
		assert result_payload["status"] == "applied"
		assert result_payload["package_script_action"] == "added"
		assert result_payload["message"] == "audit-gate assets applied"
		assert "package.json" in result_payload["changed_files"]
		assert "scripts/security/check-npm-audit.js" in result_payload["changed_files"]
		assert "security/dependency-audit-allowlist.json" in result_payload["changed_files"]

		changed_files = _read_changed_files(repo_root / ".tmp" / "audit_gate_changed_files.txt")
		assert "package.json" in changed_files
		assert "scripts/security/check-npm-audit.js" in changed_files
		assert "security/dependency-audit-allowlist.json" in changed_files

		package_payload = _read_json(repo_root / "package.json")
		assert package_payload["scripts"]["audit:ci"] == "node scripts/security/check-npm-audit.js"
		assert (repo_root / "scripts" / "security" / "check-npm-audit.js").is_file()
		assert (repo_root / "security" / "dependency-audit-allowlist.json").is_file()

		npm_probe = subprocess.run(
			["npm", "run", "audit:ci"],
			cwd=str(repo_root),
			text=True,
			capture_output=True,
			check=False,
			env=_build_npm_wrapper_env(
				repo_root=repo_root,
				audit_payload={
					"auditReportVersion": 2,
					"vulnerabilities": {},
				},
			),
		)
		assert npm_probe.returncode == 0, f"stdout:\n{npm_probe.stdout}\n\nstderr:\n{npm_probe.stderr}"
		assert "No vulnerabilities reported by npm audit." in npm_probe.stdout


def test_missing_template_asset_does_not_mutate_package_json() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-gate-missing-asset-") as td:
		repo_root = Path(td)
		_write_json(
			repo_root / "package.json",
			{
				"name": "fixture",
				"version": "1.0.0",
				"scripts": {
					"test": "echo ok"
				},
			},
		)
		before_package = (repo_root / "package.json").read_text(encoding="utf-8")

		broken_contract_root = repo_root / "broken-contract"
		shutil.copytree(CANONICAL_CONTRACT_ROOT, broken_contract_root)
		(broken_contract_root / "assets" / "scripts" / "security" / "check-npm-audit.js").unlink()

		result = _run_apply(contract_root=broken_contract_root, repo_root=repo_root)
		assert result.returncode == 1, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"

		result_payload = _read_json(repo_root / ".tmp" / "audit_gate_result.json")
		assert result_payload["status"] == "error"
		assert "Audit-gate asset missing" in result_payload["message"]
		assert _read_changed_files(repo_root / ".tmp" / "audit_gate_changed_files.txt") == []

		after_package = (repo_root / "package.json").read_text(encoding="utf-8")
		assert after_package == before_package
		assert not (repo_root / "scripts" / "security" / "check-npm-audit.js").exists()


def test_custom_audit_script_is_preserved_without_partial_asset_copy() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-gate-custom-script-") as td:
		repo_root = Path(td)
		_write_json(
			repo_root / "package.json",
			{
				"name": "fixture",
				"version": "1.0.0",
				"scripts": {
					"audit:ci": "node scripts/custom-audit-check.js"
				},
			},
		)

		result = _run_apply(contract_root=CANONICAL_CONTRACT_ROOT, repo_root=repo_root)
		assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"

		result_payload = _read_json(repo_root / ".tmp" / "audit_gate_result.json")
		assert result_payload["status"] == "custom_script_preserved"
		assert result_payload["package_script_action"] == "custom_preserved"
		assert result_payload["changed_files"] == []
		assert _read_changed_files(repo_root / ".tmp" / "audit_gate_changed_files.txt") == []

		package_payload = _read_json(repo_root / "package.json")
		assert package_payload["scripts"]["audit:ci"] == "node scripts/custom-audit-check.js"
		assert not (repo_root / "scripts" / "security" / "check-npm-audit.js").exists()


def test_reapply_is_idempotent_once_assets_are_present() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-gate-idempotent-") as td:
		repo_root = Path(td)
		_write_json(
			repo_root / "package.json",
			{
				"name": "fixture",
				"version": "1.0.0",
				"scripts": {},
			},
		)

		first = _run_apply(contract_root=CANONICAL_CONTRACT_ROOT, repo_root=repo_root)
		assert first.returncode == 0, f"stdout:\n{first.stdout}\n\nstderr:\n{first.stderr}"

		second = _run_apply(contract_root=CANONICAL_CONTRACT_ROOT, repo_root=repo_root)
		assert second.returncode == 0, f"stdout:\n{second.stdout}\n\nstderr:\n{second.stderr}"

		result_payload = _read_json(repo_root / ".tmp" / "audit_gate_result.json")
		assert result_payload["status"] == "unchanged"
		assert result_payload["package_script_action"] == "unchanged"
		assert result_payload["changed_files"] == []
		assert _read_changed_files(repo_root / ".tmp" / "audit_gate_changed_files.txt") == []


def test_package_write_failure_rolls_back_copied_assets() -> None:
	with tempfile.TemporaryDirectory(prefix="audit-gate-write-failure-") as td:
		repo_root = Path(td)
		_write_json(
			repo_root / "package.json",
			{
				"name": "fixture",
				"version": "1.0.0",
				"scripts": {},
			},
		)

		asset_file = repo_root / "scripts" / "security" / "check-npm-audit.js"
		asset_file.parent.mkdir(parents=True, exist_ok=True)
		asset_file.write_text("stale-script", encoding="utf-8")
		allowlist_file = repo_root / "security" / "dependency-audit-allowlist.json"
		allowlist_file.parent.mkdir(parents=True, exist_ok=True)
		allowlist_file.write_text("[\"stale\"]\n", encoding="utf-8")

		before_package = (repo_root / "package.json").read_text(encoding="utf-8")
		before_asset = asset_file.read_bytes()
		before_allowlist = allowlist_file.read_bytes()

		import importlib.util
		import sys

		spec = importlib.util.spec_from_file_location("apply_audit_gate_assets", APPLY_SCRIPT)
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = module
		try:
			spec.loader.exec_module(module)

			original_write_json_atomic = module._write_json_atomic
			try:
				def _fail_write_json_atomic(path: Path, payload: dict) -> None:
					raise RuntimeError("simulated package write failure")

				module._write_json_atomic = _fail_write_json_atomic
				try:
					module._apply_assets(contract_root=CANONICAL_CONTRACT_ROOT, repo_root=repo_root)
				except RuntimeError as exc:
					assert str(exc) == "simulated package write failure"
				else:
					raise AssertionError("expected simulated package write failure")
			finally:
				module._write_json_atomic = original_write_json_atomic
		finally:
			sys.modules.pop(spec.name, None)

		assert (repo_root / "package.json").read_text(encoding="utf-8") == before_package
		assert asset_file.read_bytes() == before_asset
		assert allowlist_file.read_bytes() == before_allowlist


def main() -> int:
	test_fresh_apply_vendors_script_and_package_script_atomically()
	test_missing_template_asset_does_not_mutate_package_json()
	test_custom_audit_script_is_preserved_without_partial_asset_copy()
	test_reapply_is_idempotent_once_assets_are_present()
	test_package_write_failure_rolls_back_copied_assets()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
