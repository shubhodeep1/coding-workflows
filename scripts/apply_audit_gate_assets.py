#!/usr/bin/env python3
"""Apply canonical audit-gate assets atomically to a repository."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile


CONTRACT_FILE_NAME = "contract.json"


@dataclass(frozen=True)
class ApplyAuditGateResult:
	status: str
	package_script_action: str
	changed_files: tuple[str, ...]
	message: str


def _safe_relative_path(raw: str, *, field_name: str) -> Path:
	path = Path(raw)
	if path.is_absolute() or ".." in path.parts:
		raise ValueError(f"{field_name} must be a safe relative path: {raw}")
	return path


def _load_contract(contract_root: Path) -> dict:
	contract_path = contract_root / CONTRACT_FILE_NAME
	if not contract_path.is_file():
		raise FileNotFoundError(f"Audit-gate contract missing: {contract_path.as_posix()}")
	try:
		contract = json.loads(contract_path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as exc:
		raise ValueError(f"Audit-gate contract is not valid JSON: {contract_path.as_posix()}") from exc
	if not isinstance(contract, dict):
		raise ValueError("Audit-gate contract root must be a JSON object")

	package_script = contract.get("package_script")
	if not isinstance(package_script, dict):
		raise ValueError("Audit-gate contract must include package_script object")
	if not isinstance(package_script.get("name"), str) or not package_script["name"].strip():
		raise ValueError("Audit-gate contract package_script.name must be a non-empty string")
	if not isinstance(package_script.get("value"), str) or not package_script["value"].strip():
		raise ValueError("Audit-gate contract package_script.value must be a non-empty string")

	managed_files = contract.get("managed_files")
	if not isinstance(managed_files, list) or not managed_files:
		raise ValueError("Audit-gate contract managed_files must be a non-empty array")
	for idx, item in enumerate(managed_files):
		if not isinstance(item, dict):
			raise ValueError(f"managed_files[{idx}] must be an object")
		if not isinstance(item.get("source"), str) or not item["source"].strip():
			raise ValueError(f"managed_files[{idx}].source must be a non-empty string")
		if not isinstance(item.get("target"), str) or not item["target"].strip():
			raise ValueError(f"managed_files[{idx}].target must be a non-empty string")

	return contract


def _write_json_atomic(path: Path, payload: dict) -> None:
	rendered = json.dumps(payload, indent=2) + "\n"
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp_path: Path | None = None
	try:
		with NamedTemporaryFile(
			mode="w",
			encoding="utf-8",
			dir=str(path.parent),
			prefix=f".{path.name}.",
			suffix=".tmp",
			delete=False,
		) as handle:
			handle.write(rendered)
			tmp_path = Path(handle.name)
		tmp_path.replace(path)
	except Exception:
		if tmp_path is not None and tmp_path.exists():
			try:
				tmp_path.unlink(missing_ok=True)
			except Exception:
				pass
		raise


def _apply_assets(*, contract_root: Path, repo_root: Path) -> ApplyAuditGateResult:
	contract = _load_contract(contract_root)
	package_script = contract["package_script"]
	managed_items = contract["managed_files"]

	resolved_assets: list[tuple[Path, Path]] = []
	for item in managed_items:
		source_rel = _safe_relative_path(item["source"], field_name="managed_files.source")
		target_rel = _safe_relative_path(item["target"], field_name="managed_files.target")
		source_path = contract_root / source_rel
		if not source_path.is_file():
			raise FileNotFoundError(f"Audit-gate asset missing: {source_path.as_posix()}")
		resolved_assets.append((source_path, target_rel))

	package_json_path = repo_root / "package.json"
	if not package_json_path.is_file():
		return ApplyAuditGateResult(
			status="no_package_json",
			package_script_action="not_applicable",
			changed_files=(),
			message="package.json is absent; audit-gate apply skipped",
		)

	try:
		package_payload = json.loads(package_json_path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as exc:
		raise ValueError(f"package.json is not valid JSON: {package_json_path.as_posix()}") from exc
	if not isinstance(package_payload, dict):
		raise ValueError("package.json root must be a JSON object")

	scripts = package_payload.get("scripts")
	if scripts is None:
		scripts = {}
		package_payload["scripts"] = scripts
	if not isinstance(scripts, dict):
		raise ValueError("package.json scripts field must be an object when present")

	script_name = package_script["name"]
	script_value = package_script["value"]
	existing_script_value = scripts.get(script_name)
	if existing_script_value is not None and existing_script_value != script_value:
		return ApplyAuditGateResult(
			status="custom_script_preserved",
			package_script_action="custom_preserved",
			changed_files=(),
			message=f"existing scripts.{script_name} is custom and was preserved",
		)

	asset_changed_files: list[str] = []
	asset_rollbacks: list[tuple[Path, bytes | None]] = []
	created_dirs: set[Path] = set()
	package_script_action = "unchanged"

	try:
		for source_path, target_rel in resolved_assets:
			target_path = repo_root / target_rel
			target_dir = target_path.parent
			missing_dirs: list[Path] = []
			probe_dir = target_dir
			while probe_dir != repo_root and not probe_dir.exists():
				missing_dirs.append(probe_dir)
				probe_dir = probe_dir.parent
			for missing_dir in reversed(missing_dirs):
				created_dirs.add(missing_dir)
			target_dir.mkdir(parents=True, exist_ok=True)
			target_before = target_path.read_bytes() if target_path.exists() else None
			source_bytes = source_path.read_bytes()
			if target_before != source_bytes:
				shutil.copy2(source_path, target_path)
				asset_changed_files.append(target_rel.as_posix())
				asset_rollbacks.append((target_path, target_before))

		if existing_script_value is None:
			scripts[script_name] = script_value
			_write_json_atomic(package_json_path, package_payload)
			package_script_action = "added"
	except Exception:
		rollback_failures: list[str] = []
		for target_path, target_before in reversed(asset_rollbacks):
			try:
				if target_before is None:
					target_path.unlink(missing_ok=True)
				else:
					target_path.write_bytes(target_before)
			except Exception as rollback_exc:
				rollback_failures.append(f"{target_path.as_posix()}: {rollback_exc}")
		for created_dir in sorted(created_dirs, key=lambda path: len(path.parts), reverse=True):
			try:
				created_dir.rmdir()
			except Exception as rollback_exc:
				rollback_failures.append(f"{created_dir.as_posix()}: {rollback_exc}")
		if rollback_failures:
			print(
				"warning: audit-gate rollback encountered errors: " + "; ".join(rollback_failures),
				file=sys.stderr,
			)
		raise

	changed_files = list(asset_changed_files)
	if package_script_action == "added":
		changed_files.append("package.json")

	if changed_files:
		status = "applied"
		message = "audit-gate assets applied"
	else:
		status = "unchanged"
		message = "audit-gate already aligned"

	return ApplyAuditGateResult(
		status=status,
		package_script_action=package_script_action,
		changed_files=tuple(changed_files),
		message=message,
	)


def _write_changed_files(path: Path, changed_files: tuple[str, ...]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	if changed_files:
		path.write_text("\n".join(changed_files) + "\n", encoding="utf-8")
	else:
		path.write_text("", encoding="utf-8")


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--contract-root", required=True, help="Path to canonical audit-gate contract directory")
	parser.add_argument("--repo-root", required=True, help="Path to repository to mutate")
	parser.add_argument("--result-file", required=True, help="Path for JSON apply result")
	parser.add_argument(
		"--changed-files-file",
		required=True,
		help="Path for newline-delimited changed files list",
	)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	contract_root = Path(args.contract_root).resolve()
	repo_root = Path(args.repo_root).resolve()
	result_file = Path(args.result_file).resolve()
	changed_files_file = Path(args.changed_files_file).resolve()

	try:
		result = _apply_assets(contract_root=contract_root, repo_root=repo_root)
	except Exception as exc:  # pylint: disable=broad-except
		error_payload = {
			"status": "error",
			"package_script_action": "not_run",
			"changed_files": [],
			"message": str(exc),
		}
		result_file.parent.mkdir(parents=True, exist_ok=True)
		result_file.write_text(json.dumps(error_payload, indent=2) + "\n", encoding="utf-8")
		_write_changed_files(changed_files_file, ())
		print(json.dumps(error_payload, sort_keys=True))
		return 1

	payload = asdict(result)
	result_file.parent.mkdir(parents=True, exist_ok=True)
	result_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
	_write_changed_files(changed_files_file, result.changed_files)
	print(json.dumps(payload, sort_keys=True))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
