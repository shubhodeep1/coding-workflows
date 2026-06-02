#!/usr/bin/env python3
"""Render prompt templates with optional mode contracts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
	import yaml
except ImportError:  # pragma: no cover - dependency is optional
	yaml = None


PLACEHOLDER_PATTERN = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
STANDALONE_PLACEHOLDER_PATTERN = re.compile(r"^[ \t]*\{\{([A-Za-z0-9_]+)\}\}[ \t]*$")
VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
CONTRACT_TOP_LEVEL_KEYS = {"required_vars", "optional_vars", "forbidden_vars"}


class RenderPromptError(Exception):
	"""Base error class for prompt rendering failures."""


class PromptLoadError(RenderPromptError):
	"""Raised when the prompt file cannot be loaded."""


class CliValueError(RenderPromptError):
	"""Raised when CLI-provided variable mappings are invalid."""


class ContractLoadError(RenderPromptError):
	"""Raised when a prompt contract cannot be loaded or validated."""


class ContractViolationError(RenderPromptError):
	"""Raised when a prompt violates an applicable contract."""


@dataclass(frozen=True)
class PromptContract:
	"""Strict-mode contract for a prompt mode."""

	mode_name: str
	path: Path
	required_vars: tuple[str, ...]
	optional_vars: dict[str, str]
	forbidden_vars: tuple[str, ...]

	@property
	def allowed_vars(self) -> set[str]:
		return set(self.required_vars) | set(self.optional_vars.keys())


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Render prompt templates with optional mode contracts")
	parser.add_argument("prompt_file", help="Path to the prompt template file")
	parser.add_argument(
		"--legacy-mode-name",
		default=None,
		help="Optional mode-name override for legacy shims",
	)
	parser.add_argument(
		"--var",
		dest="variables",
		action="append",
		default=[],
		metavar="NAME=VALUE",
		help="Placeholder assignment; may be passed multiple times",
	)
	return parser


def _normalize_prompt_text(content: str) -> str:
	normalized = content.replace("\r\n", "\n").replace("\r", "\n")
	if not normalized.endswith("\n"):
		normalized += "\n"
	return normalized


def load_prompt(prompt_path: Path) -> str:
	if not prompt_path.is_file():
		raise PromptLoadError(f"Prompt file not found: {prompt_path}")
	try:
		return _normalize_prompt_text(prompt_path.read_text(encoding="utf-8"))
	except OSError as exc:
		raise PromptLoadError(f"Unable to read prompt file '{prompt_path}': {exc}") from exc


def resolve_mode_name(prompt_path: Path, legacy_mode_name: str | None) -> str:
	if legacy_mode_name is not None:
		mode_name = legacy_mode_name.strip()
	else:
		mode_name = prompt_path.stem.strip()
	if not mode_name:
		raise CliValueError(f"Unable to resolve mode name for prompt '{prompt_path}'")
	return mode_name


def parse_cli_variables(entries: list[str]) -> dict[str, str]:
	variables: dict[str, str] = {}
	for entry in entries:
		name, separator, value = entry.partition("=")
		if separator != "=":
			raise CliValueError(f"Invalid --var value '{entry}'; expected NAME=VALUE")
		name = name.strip()
		if not name:
			raise CliValueError(f"Invalid --var value '{entry}'; variable name is empty")
		if VARIABLE_NAME_PATTERN.fullmatch(name) is None:
			raise CliValueError(
				f"Invalid --var name '{name}'; names must match {VARIABLE_NAME_PATTERN.pattern}"
			)
		variables[name] = value
	return variables


def _append_contract_candidate(
	candidates: list[Path],
	seen: set[Path],
	base_dir: Path | None,
	mode_name: str,
) -> None:
	if base_dir is None:
		return
	candidate = (base_dir / "prompts" / "contracts" / f"{mode_name}.yml").resolve()
	if candidate in seen:
		return
	seen.add(candidate)
	candidates.append(candidate)


def discover_contract_path(prompt_path: Path, mode_name: str) -> Path | None:
	candidates: list[Path] = []
	seen: set[Path] = set()
	script_root = Path(__file__).resolve().parents[1]
	prompt_root = prompt_path.parent.parent if prompt_path.parent.name == "prompts" else None

	_append_contract_candidate(candidates, seen, prompt_root, mode_name)
	_append_contract_candidate(candidates, seen, Path.cwd(), mode_name)
	_append_contract_candidate(candidates, seen, script_root, mode_name)
	_append_contract_candidate(candidates, seen, Path.cwd() / ".codex-workflow-src", mode_name)
	_append_contract_candidate(candidates, seen, Path.cwd() / ".codex-workflow-src-main", mode_name)

	for candidate in candidates:
		if candidate.is_file():
			return candidate
	return None


def _parse_scalar_value(raw_value: str, *, field_name: str, line_number: int, path: Path) -> str:
	if raw_value == "":
		return ""
	if raw_value.startswith('"'):
		try:
			parsed = json.loads(raw_value)
		except json.JSONDecodeError as exc:
			raise ContractLoadError(
				f"Invalid JSON-style quoted scalar for {field_name} at {path}:{line_number}: {exc.msg}"
			) from exc
		if not isinstance(parsed, str):
			raise ContractLoadError(
				f"Expected string scalar for {field_name} at {path}:{line_number}, got {type(parsed).__name__}"
			)
		return parsed
	if raw_value.startswith("'") and raw_value.endswith("'") and len(raw_value) >= 2:
		return raw_value[1:-1].replace("''", "'")
	return raw_value


def _parse_inline_list(raw_value: str, *, field_name: str, path: Path, line_number: int) -> list[str]:
	if raw_value == "[]":
		return []
	try:
		parsed = json.loads(raw_value)
	except json.JSONDecodeError as exc:
		raise ContractLoadError(
			f"Invalid inline list for {field_name} at {path}:{line_number}: {exc.msg}"
		) from exc
	if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
		raise ContractLoadError(
			f"Expected {field_name} to be a list of strings at {path}:{line_number}"
		)
	return parsed


def _parse_minimal_contract_yaml(text: str, path: Path) -> dict[str, Any]:
	stripped = text.strip()
	if not stripped:
		return {}
	if stripped.startswith("{"):
		try:
			loaded = json.loads(stripped)
		except json.JSONDecodeError as exc:
			raise ContractLoadError(
				f"Invalid JSON-formatted contract '{path}' at line {exc.lineno}, column {exc.colno}: {exc.msg}"
			) from exc
		if not isinstance(loaded, dict):
			raise ContractLoadError(f"Contract root must be a mapping/object in '{path}'")
		return loaded

	data: dict[str, Any] = {}
	lines = text.splitlines()
	index = 0
	while index < len(lines):
		raw_line = lines[index]
		line_number = index + 1
		stripped_line = raw_line.strip()
		if not stripped_line or stripped_line.startswith("#"):
			index += 1
			continue
		if raw_line.startswith((" ", "\t")):
			raise ContractLoadError(
				f"Unsupported indentation at {path}:{line_number}; install PyYAML for richer contract syntax"
			)
		key, separator, remainder = raw_line.partition(":")
		if separator != ":":
			raise ContractLoadError(f"Invalid contract line at {path}:{line_number}: {raw_line!r}")
		key = key.strip()
		remainder = remainder.strip()
		if key not in CONTRACT_TOP_LEVEL_KEYS:
			raise ContractLoadError(
				f"Unknown contract key '{key}' in '{path}'; supported keys: {sorted(CONTRACT_TOP_LEVEL_KEYS)}"
			)

		if key in {"required_vars", "forbidden_vars"}:
			if remainder:
				data[key] = _parse_inline_list(remainder, field_name=key, path=path, line_number=line_number)
				index += 1
				continue
			items: list[str] = []
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
				child_payload = child_raw[2:]
				if not child_payload.startswith("- "):
					raise ContractLoadError(
						f"Expected list item under {key} at {path}:{child_line_number}"
					)
				items.append(
					_parse_scalar_value(
						child_payload[2:].strip(),
						field_name=key,
						line_number=child_line_number,
						path=path,
					)
				)
				index += 1
			data[key] = items
			continue

		if remainder:
			if remainder == "{}":
				data[key] = {}
				index += 1
				continue
			raise ContractLoadError(
				f"Unsupported inline mapping syntax for {key} at {path}:{line_number}; install PyYAML for richer contract syntax"
			)

		mapping: dict[str, str] = {}
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
			child_payload = child_raw[2:]
			child_key, child_separator, child_remainder = child_payload.partition(":")
			if child_separator != ":":
				raise ContractLoadError(
					f"Expected key/value mapping under {key} at {path}:{child_line_number}"
				)
			mapping[child_key.strip()] = _parse_scalar_value(
				child_remainder.strip(),
				field_name=f"{key}.{child_key.strip()}",
				line_number=child_line_number,
				path=path,
			)
			index += 1
		data[key] = mapping

	return data


def _load_contract_payload(contract_path: Path) -> dict[str, Any]:
	try:
		contract_text = contract_path.read_text(encoding="utf-8")
	except OSError as exc:
		raise ContractLoadError(f"Unable to read contract '{contract_path}': {exc}") from exc

	if yaml is not None:
		try:
			loaded = yaml.safe_load(contract_text)
		except yaml.YAMLError as exc:
			location = ""
			mark = getattr(exc, "problem_mark", None)
			if mark is not None:
				location = f" at line {mark.line + 1}, column {mark.column + 1}"
			raise ContractLoadError(
				f"Failed to parse contract '{contract_path}'{location}: {exc}"
			) from exc
	else:
		loaded = _parse_minimal_contract_yaml(contract_text, contract_path)

	if loaded is None:
		loaded = {}
	if not isinstance(loaded, dict):
		raise ContractLoadError(
			f"Contract root must be a mapping/object in '{contract_path}', got {type(loaded).__name__}"
		)
	return loaded


def _coerce_name_list(value: Any, *, field_name: str, contract_path: Path) -> tuple[str, ...]:
	if value is None:
		value = []
	if not isinstance(value, list):
		raise ContractLoadError(f"Field '{field_name}' in '{contract_path}' must be a list of strings")
	seen: set[str] = set()
	items: list[str] = []
	for item in value:
		if not isinstance(item, str):
			raise ContractLoadError(
				f"Field '{field_name}' in '{contract_path}' must contain only strings"
			)
		name = item.strip()
		if not name:
			raise ContractLoadError(f"Field '{field_name}' in '{contract_path}' contains an empty variable name")
		if VARIABLE_NAME_PATTERN.fullmatch(name) is None:
			raise ContractLoadError(
				f"Field '{field_name}' in '{contract_path}' contains invalid variable name '{name}'"
			)
		if name in seen:
			continue
		seen.add(name)
		items.append(name)
	return tuple(items)


def _coerce_optional_vars(value: Any, *, contract_path: Path) -> dict[str, str]:
	if value is None:
		value = {}
	if not isinstance(value, dict):
		raise ContractLoadError(f"Field 'optional_vars' in '{contract_path}' must be a mapping")
	optional_vars: dict[str, str] = {}
	for raw_name, raw_default in value.items():
		if not isinstance(raw_name, str):
			raise ContractLoadError(
				f"Field 'optional_vars' in '{contract_path}' must use string variable names"
			)
		name = raw_name.strip()
		if not name:
			raise ContractLoadError(f"Field 'optional_vars' in '{contract_path}' contains an empty variable name")
		if VARIABLE_NAME_PATTERN.fullmatch(name) is None:
			raise ContractLoadError(
				f"Field 'optional_vars' in '{contract_path}' contains invalid variable name '{name}'"
			)
		if raw_default is None:
			default_value = ""
		elif isinstance(raw_default, str):
			default_value = raw_default
		else:
			raise ContractLoadError(
				f"Field 'optional_vars.{name}' in '{contract_path}' must be a string default"
			)
		optional_vars[name] = default_value
	return optional_vars


def load_contract(contract_path: Path, mode_name: str) -> PromptContract:
	payload = _load_contract_payload(contract_path)
	unknown_keys = sorted(set(payload.keys()) - CONTRACT_TOP_LEVEL_KEYS)
	if unknown_keys:
		raise ContractLoadError(
			f"Unknown contract keys in '{contract_path}': {', '.join(unknown_keys)}"
		)

	required_vars = _coerce_name_list(payload.get("required_vars", []), field_name="required_vars", contract_path=contract_path)
	optional_vars = _coerce_optional_vars(payload.get("optional_vars", {}), contract_path=contract_path)
	forbidden_vars = _coerce_name_list(payload.get("forbidden_vars", []), field_name="forbidden_vars", contract_path=contract_path)

	overlap = (set(required_vars) | set(optional_vars.keys())) & set(forbidden_vars)
	if overlap:
		raise ContractLoadError(
			f"Contract '{contract_path}' declares variables in both allow and forbid lists: {', '.join(sorted(overlap))}"
		)

	return PromptContract(
		mode_name=mode_name,
		path=contract_path,
		required_vars=required_vars,
		optional_vars=optional_vars,
		forbidden_vars=forbidden_vars,
	)


def collect_placeholders(prompt_text: str) -> tuple[str, ...]:
	return tuple(sorted(set(PLACEHOLDER_PATTERN.findall(prompt_text))))


def validate_contract(contract: PromptContract, prompt_text: str, values: dict[str, str]) -> None:
	placeholders = collect_placeholders(prompt_text)
	violations: list[tuple[str, list[str]]] = []

	forbidden_present = sorted(name for name in placeholders if name in set(contract.forbidden_vars))
	if forbidden_present:
		violations.append(("forbidden_present", forbidden_present))

	unknown_in_template = sorted(
		name
		for name in placeholders
		if name not in contract.allowed_vars and name not in set(contract.forbidden_vars)
	)
	if unknown_in_template:
		violations.append(("unknown_in_template", unknown_in_template))

	missing_required = sorted(name for name in contract.required_vars if name not in values)
	if missing_required:
		violations.append(("missing_required", missing_required))

	if not violations:
		return

	detail_lines = [f"- {category}: {', '.join(names)}" for category, names in violations]
	raise ContractViolationError(
		f"Prompt contract violations for mode '{contract.mode_name}' via '{contract.path}':\n"
		+ "\n".join(detail_lines)
	)


def _render_replacement(value: str) -> str:
	if value.endswith("\n"):
		return value
	return value + "\n"


def render_prompt_text(prompt_text: str, values: dict[str, str]) -> str:
	rendered_lines: list[str] = []
	for line in prompt_text.splitlines():
		match = STANDALONE_PLACEHOLDER_PATTERN.fullmatch(line)
		if match is None:
			rendered_lines.append(f"{line}\n")
			continue
		placeholder_name = match.group(1)
		if placeholder_name not in values:
			rendered_lines.append(f"{line}\n")
			continue
		rendered_lines.append(_render_replacement(values[placeholder_name]))
	return "".join(rendered_lines)


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	prompt_path = Path(args.prompt_file)

	try:
		prompt_text = load_prompt(prompt_path)
		mode_name = resolve_mode_name(prompt_path, args.legacy_mode_name)
		provided_values = parse_cli_variables(args.variables)
		contract_path = discover_contract_path(prompt_path, mode_name)
		if contract_path is not None:
			contract = load_contract(contract_path, mode_name)
			effective_values = dict(contract.optional_vars)
			effective_values.update(provided_values)
			validate_contract(contract, prompt_text, effective_values)
		else:
			effective_values = dict(provided_values)
		sys.stdout.write(render_prompt_text(prompt_text, effective_values))
	except RenderPromptError as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 1

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
