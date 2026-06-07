#!/usr/bin/env python3
"""Load .github/ai/WORKFLOW.md prompt overrides into $GITHUB_ENV."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
	import yaml
except ImportError:  # pragma: no cover - dependency is optional
	yaml = None

try:
	from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - dependency is optional
	Draft202012Validator = None


WORKFLOW_OVERLAY_RELATIVE_PATH = Path(".github/ai/WORKFLOW.md")
WORKFLOW_SCHEMA_VERSION = "workflow_overlay.v1"
OVERLAY_TOP_LEVEL_KEYS = {"schema_version", "prompt_overrides"}
PROMPT_OVERRIDE_KEYS = {"mode", "append_path", "replace_path"}
OVERLAY_MODE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$")


class WorkflowOverlayLoadError(Exception):
	"""Raised when WORKFLOW.md cannot be parsed or validated."""


@dataclass(frozen=True)
class PromptOverride:
	"""One validated prompt override entry."""

	mode_name: str
	append_path: str | None
	replace_path: str | None


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Load .github/ai/WORKFLOW.md into $GITHUB_ENV")
	parser.add_argument("--repo-root", required=True, help="Repository root containing .github/ai/WORKFLOW.md")
	parser.add_argument("--schema-path", required=True, help="Path to ai-memory/schemas/workflow_overlay.v1.json")
	parser.add_argument(
		"--github-env",
		default=os.environ.get("GITHUB_ENV", ""),
		help="Path to the GitHub Actions env file (defaults to $GITHUB_ENV)",
	)
	return parser


def _normalize_text(content: str) -> str:
	normalized = content.replace("\r\n", "\n").replace("\r", "\n")
	if not normalized.endswith("\n"):
		normalized += "\n"
	return normalized


def resolve_repo_root(raw_value: str) -> Path:
	repo_root = Path(raw_value).resolve()
	if not repo_root.exists():
		raise WorkflowOverlayLoadError(f"Repository root does not exist: {raw_value}")
	if not repo_root.is_dir():
		raise WorkflowOverlayLoadError(f"Repository root is not a directory: {raw_value}")
	return repo_root


def load_overlay_document(repo_root: Path) -> str | None:
	overlay_path = repo_root / WORKFLOW_OVERLAY_RELATIVE_PATH
	if not overlay_path.is_file():
		return None
	try:
		return _normalize_text(overlay_path.read_text(encoding="utf-8"))
	except OSError as exc:
		raise WorkflowOverlayLoadError(f"Unable to read '{overlay_path}': {exc}") from exc


def extract_front_matter(document_text: str, overlay_path: Path) -> str | None:
	if not document_text.startswith("---\n"):
		return None
	lines = document_text.splitlines()
	for index in range(1, len(lines)):
		if lines[index].strip() == "---":
			return "\n".join(lines[1:index])
	raise WorkflowOverlayLoadError(
		f"Unterminated YAML front matter in '{overlay_path}'; expected a closing --- line"
	)


def _parse_scalar_value(raw_value: str, *, field_name: str, line_number: int, path: Path) -> str:
	if raw_value == "":
		return ""
	if raw_value.startswith('"'):
		try:
			parsed = json.loads(raw_value)
		except json.JSONDecodeError as exc:
			raise WorkflowOverlayLoadError(
				f"Invalid quoted scalar for {field_name} at {path}:{line_number}: {exc.msg}"
			) from exc
		if not isinstance(parsed, str):
			raise WorkflowOverlayLoadError(
				f"Expected string scalar for {field_name} at {path}:{line_number}"
			)
		return parsed
	if raw_value.startswith("'") and raw_value.endswith("'") and len(raw_value) >= 2:
		return raw_value[1:-1].replace("''", "'")
	return raw_value


def _parse_minimal_front_matter(front_matter_text: str, overlay_path: Path) -> dict[str, Any]:
	stripped = front_matter_text.strip()
	if not stripped:
		return {}
	if stripped.startswith("{"):
		try:
			loaded = json.loads(stripped)
		except json.JSONDecodeError as exc:
			raise WorkflowOverlayLoadError(
				f"Invalid JSON-formatted front matter in '{overlay_path}' at line {exc.lineno}, column {exc.colno}: {exc.msg}"
			) from exc
		if not isinstance(loaded, dict):
			raise WorkflowOverlayLoadError(f"Front matter root must be an object in '{overlay_path}'")
		return loaded

	data: dict[str, Any] = {}
	lines = front_matter_text.splitlines()
	index = 0
	while index < len(lines):
		raw_line = lines[index]
		line_number = index + 1
		stripped_line = raw_line.strip()
		if not stripped_line or stripped_line.startswith("#"):
			index += 1
			continue
		if raw_line.startswith((" ", "\t")):
			raise WorkflowOverlayLoadError(
				f"Unsupported indentation at {overlay_path}:{line_number}; install PyYAML for richer WORKFLOW.md syntax"
			)
		key, separator, remainder = raw_line.partition(":")
		if separator != ":":
			raise WorkflowOverlayLoadError(
				f"Invalid front-matter line at {overlay_path}:{line_number}: {raw_line!r}"
			)
		key = key.strip()
		remainder = remainder.strip()
		if key not in OVERLAY_TOP_LEVEL_KEYS:
			raise WorkflowOverlayLoadError(
				f"Unknown WORKFLOW overlay key '{key}' in '{overlay_path}'"
			)

		if key == "schema_version":
			if not remainder:
				raise WorkflowOverlayLoadError(
					f"Field 'schema_version' must not be empty in '{overlay_path}'"
				)
			data[key] = _parse_scalar_value(
				remainder,
				field_name=key,
				line_number=line_number,
				path=overlay_path,
			)
			index += 1
			continue

		if remainder:
			if remainder == "[]":
				data[key] = []
				index += 1
				continue
			raise WorkflowOverlayLoadError(
				f"Unsupported inline syntax for {key} at {overlay_path}:{line_number}; install PyYAML for richer WORKFLOW.md syntax"
			)

		items: list[dict[str, Any]] = []
		index += 1
		while index < len(lines):
			child_raw = lines[index]
			child_line_number = index + 1
			child_stripped = child_raw.strip()
			if not child_stripped or child_stripped.startswith("#"):
				index += 1
				continue
			if not child_raw.startswith("  "):
				break
			if not child_raw.startswith("  -"):
				raise WorkflowOverlayLoadError(
					f"Expected list item under {key} at {overlay_path}:{child_line_number}"
				)

			entry: dict[str, Any] = {}
			inline_payload = child_raw[3:].strip()
			if inline_payload:
				inline_key, inline_separator, inline_remainder = inline_payload.partition(":")
				if inline_separator != ":":
					raise WorkflowOverlayLoadError(
						f"Expected key/value mapping under {key} at {overlay_path}:{child_line_number}"
					)
				entry[inline_key.strip()] = _parse_scalar_value(
					inline_remainder.strip(),
					field_name=f"{key}[].{inline_key.strip()}",
					line_number=child_line_number,
					path=overlay_path,
				)
			index += 1

			while index < len(lines):
				nested_raw = lines[index]
				nested_line_number = index + 1
				nested_stripped = nested_raw.strip()
				if not nested_stripped or nested_stripped.startswith("#"):
					index += 1
					continue
				if not nested_raw.startswith("    "):
					break
				nested_payload = nested_raw[4:]
				nested_key, nested_separator, nested_remainder = nested_payload.partition(":")
				if nested_separator != ":":
					raise WorkflowOverlayLoadError(
						f"Expected key/value mapping under {key} at {overlay_path}:{nested_line_number}"
					)
				entry[nested_key.strip()] = _parse_scalar_value(
					nested_remainder.strip(),
					field_name=f"{key}[].{nested_key.strip()}",
					line_number=nested_line_number,
					path=overlay_path,
				)
				index += 1

			items.append(entry)
		data[key] = items

	return data


def parse_front_matter(front_matter_text: str | None, overlay_path: Path) -> dict[str, Any]:
	if front_matter_text is None:
		return {}
	if yaml is not None:
		try:
			loaded = yaml.safe_load(front_matter_text)
		except yaml.YAMLError as exc:
			location = ""
			mark = getattr(exc, "problem_mark", None)
			if mark is not None:
				location = f" at line {mark.line + 1}, column {mark.column + 1}"
			raise WorkflowOverlayLoadError(
				f"Failed to parse WORKFLOW.md front matter in '{overlay_path}'{location}: {exc}"
			) from exc
		if loaded is None:
			return {}
		if not isinstance(loaded, dict):
			raise WorkflowOverlayLoadError(f"WORKFLOW.md front matter root must be a mapping in '{overlay_path}'")
		return loaded
	return _parse_minimal_front_matter(front_matter_text, overlay_path)


def load_schema(schema_path: Path) -> dict[str, Any]:
	if not schema_path.is_file():
		raise WorkflowOverlayLoadError(f"Workflow overlay schema not found: {schema_path}")
	try:
		schema = json.loads(schema_path.read_text(encoding="utf-8"))
	except OSError as exc:
		raise WorkflowOverlayLoadError(f"Unable to read workflow overlay schema '{schema_path}': {exc}") from exc
	except json.JSONDecodeError as exc:
		raise WorkflowOverlayLoadError(
			f"Workflow overlay schema '{schema_path}' is not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
		) from exc
	if not isinstance(schema, dict):
		raise WorkflowOverlayLoadError(f"Workflow overlay schema root must be an object in '{schema_path}'")
	return schema


def validate_payload_against_schema(payload: dict[str, Any], schema: dict[str, Any], schema_path: Path) -> None:
	if Draft202012Validator is None:
		return
	try:
		Draft202012Validator.check_schema(schema)
	except Exception as exc:  # pragma: no cover - schema is repo-owned and static
		raise WorkflowOverlayLoadError(f"Workflow overlay schema '{schema_path}' is invalid: {exc}") from exc
	validator = Draft202012Validator(schema)
	errors = sorted(
		validator.iter_errors(payload),
		key=lambda error: ([str(part) for part in error.absolute_path], error.message),
	)
	if not errors:
		return
	first_error = errors[0]
	location = ".".join(str(part) for part in first_error.absolute_path) or "<root>"
	raise WorkflowOverlayLoadError(
		f"WORKFLOW overlay failed schema validation at {location}: {first_error.message}"
	)


def _coerce_mode_name(raw_value: Any, *, field_name: str, overlay_path: Path) -> str:
	if not isinstance(raw_value, str):
		raise WorkflowOverlayLoadError(f"Field '{field_name}' in '{overlay_path}' must be a string")
	mode_name = raw_value.strip()
	if not mode_name:
		raise WorkflowOverlayLoadError(f"Field '{field_name}' in '{overlay_path}' must not be empty")
	if OVERLAY_MODE_NAME_PATTERN.fullmatch(mode_name) is None:
		raise WorkflowOverlayLoadError(
			f"Field '{field_name}' in '{overlay_path}' contains invalid mode name '{mode_name}'"
		)
	return mode_name


def _coerce_optional_path(raw_value: Any, *, field_name: str, overlay_path: Path) -> str | None:
	if raw_value is None:
		return None
	if not isinstance(raw_value, str):
		raise WorkflowOverlayLoadError(f"Field '{field_name}' in '{overlay_path}' must be a string")
	path_value = raw_value.strip()
	if not path_value:
		raise WorkflowOverlayLoadError(f"Field '{field_name}' in '{overlay_path}' must not be empty")
	return path_value


def coerce_prompt_overrides(payload: dict[str, Any], overlay_path: Path) -> tuple[PromptOverride, ...]:
	unknown_keys = sorted(set(payload.keys()) - OVERLAY_TOP_LEVEL_KEYS)
	if unknown_keys:
		raise WorkflowOverlayLoadError(
			f"Unknown WORKFLOW overlay keys in '{overlay_path}': {', '.join(unknown_keys)}"
		)

	if payload:
		schema_version = payload.get("schema_version")
		if schema_version != WORKFLOW_SCHEMA_VERSION:
			raise WorkflowOverlayLoadError(
				f"Field 'schema_version' in '{overlay_path}' must be '{WORKFLOW_SCHEMA_VERSION}'"
			)

	raw_overrides = payload.get("prompt_overrides", [])
	if not isinstance(raw_overrides, list):
		raise WorkflowOverlayLoadError(
			f"Field 'prompt_overrides' in '{overlay_path}' must be a list"
		)

	overrides: list[PromptOverride] = []
	for index, item in enumerate(raw_overrides):
		if not isinstance(item, dict):
			raise WorkflowOverlayLoadError(
				f"prompt_overrides[{index}] in '{overlay_path}' must be an object"
			)
		unknown_keys = sorted(set(item.keys()) - PROMPT_OVERRIDE_KEYS)
		if unknown_keys:
			raise WorkflowOverlayLoadError(
				f"prompt_overrides[{index}] in '{overlay_path}' contains unknown keys: {', '.join(unknown_keys)}"
			)
		mode_name = _coerce_mode_name(
			item.get("mode"),
			field_name=f"prompt_overrides[{index}].mode",
			overlay_path=overlay_path,
		)
		append_path = _coerce_optional_path(
			item.get("append_path"),
			field_name=f"prompt_overrides[{index}].append_path",
			overlay_path=overlay_path,
		)
		replace_path = _coerce_optional_path(
			item.get("replace_path"),
			field_name=f"prompt_overrides[{index}].replace_path",
			overlay_path=overlay_path,
		)
		if (append_path is None) == (replace_path is None):
			raise WorkflowOverlayLoadError(
				f"prompt_overrides[{index}] in '{overlay_path}' must set exactly one of append_path or replace_path"
			)
		overrides.append(
			PromptOverride(
				mode_name=mode_name,
				append_path=append_path,
				replace_path=replace_path,
			)
		)
	return tuple(overrides)


def render_export_values(*, overlay_enabled: bool, repo_root: Path, overrides: tuple[PromptOverride, ...]) -> dict[str, str]:
	if not overlay_enabled:
		return {
			"WORKFLOW_OVERLAY_ENABLED": "false",
			"WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON": "",
			"WORKFLOW_OVERLAY_REPO_ROOT": "",
		}
	return {
		"WORKFLOW_OVERLAY_ENABLED": "true",
		"WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON": json.dumps(
			[
				{
					"mode": override.mode_name,
					**({"append_path": override.append_path} if override.append_path is not None else {}),
					**({"replace_path": override.replace_path} if override.replace_path is not None else {}),
				}
				for override in overrides
			],
			separators=(",", ":"),
		),
		"WORKFLOW_OVERLAY_REPO_ROOT": str(repo_root),
	}


def append_github_env(github_env_path: Path, values: dict[str, str]) -> None:
	try:
		with github_env_path.open("a", encoding="utf-8") as handle:
			for name, value in values.items():
				handle.write(f"{name}={value}\n")
	except OSError as exc:
		raise WorkflowOverlayLoadError(f"Unable to write GitHub env file '{github_env_path}': {exc}") from exc


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	if not args.github_env:
		print("ERROR: --github-env is required when $GITHUB_ENV is unset", file=sys.stderr)
		return 1

	try:
		repo_root = resolve_repo_root(args.repo_root)
		overlay_path = repo_root / WORKFLOW_OVERLAY_RELATIVE_PATH
		document_text = load_overlay_document(repo_root)
		if document_text is None:
			append_github_env(
				Path(args.github_env),
				render_export_values(overlay_enabled=False, repo_root=repo_root, overrides=()),
			)
			return 0

		front_matter_text = extract_front_matter(document_text, overlay_path)
		payload = parse_front_matter(front_matter_text, overlay_path)
		schema = load_schema(Path(args.schema_path))
		validate_payload_against_schema(payload, schema, Path(args.schema_path))
		overrides = coerce_prompt_overrides(payload, overlay_path)
		append_github_env(
			Path(args.github_env),
			render_export_values(overlay_enabled=True, repo_root=repo_root, overrides=overrides),
		)
	except WorkflowOverlayLoadError as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 1

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
