#!/usr/bin/env python3
"""Render prompt templates with optional mode contracts."""

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


PLACEHOLDER_PATTERN = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
PLACEHOLDER_EXPRESSION_PATTERN = re.compile(r"\{\{(.*?)\}\}")
STANDALONE_PLACEHOLDER_PATTERN = re.compile(r"^[ \t]*\{\{([A-Za-z0-9_]+)\}\}[ \t]*$")
INCLUDE_DIRECTIVE_PATTERN = re.compile(r'^[ \t]*\{%[ \t]*include[ \t]+"([^"\n]+)"[ \t]*%\}[ \t]*$')
VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
LITERAL_DOT_EXPRESSION_PATTERN = re.compile(r"^\s*\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\s*$")
OVERLAY_MODE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$")
CONTRACT_TOP_LEVEL_KEYS = {"required_vars", "optional_vars", "forbidden_vars"}
PERSONA_SOURCE_FILE_NAME = "_prelude_role_persona.txt"
LEGACY_STANDALONE_ONLY_VARS = frozenset({"WORKFLOW_EDIT_RESTRICTION", "SEMBLE_PREFETCH", "SERENA_TOOL_HINTS"})
FALSEY_ENV_VALUES = frozenset({"0", "false", "no", "off"})


class RenderPromptError(Exception):
	"""Base error class for prompt rendering failures."""


class PromptLoadError(RenderPromptError):
	"""Raised when the prompt file cannot be loaded."""


class TemplateSyntaxError(RenderPromptError):
	"""Raised when unsupported template syntax is present in a prompt."""


class PromptAssemblyError(RenderPromptError):
	"""Raised when a prompt template include cannot be assembled."""


class CliValueError(RenderPromptError):
	"""Raised when CLI-provided variable mappings are invalid."""


class ContractLoadError(RenderPromptError):
	"""Raised when a prompt contract cannot be loaded or validated."""


class ContractViolationError(RenderPromptError):
	"""Raised when a prompt violates an applicable contract."""


class WorkflowOverlayError(RenderPromptError):
	"""Raised when workflow-overlay prompt overrides are invalid."""


@dataclass(frozen=True)
class PromptFragment:
	"""One prompt fragment before include assembly."""

	path: Path
	text: str


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


@dataclass(frozen=True)
class PromptOverride:
	"""One workflow-overlay prompt override entry."""

	mode_name: str
	append_path: str | None
	replace_path: str | None


@dataclass(frozen=True)
class WorkflowOverlay:
	"""Runtime workflow overlay exported via environment variables."""

	repo_root: Path
	overrides: tuple[PromptOverride, ...]


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Render prompt templates with optional mode contracts")
	parser.add_argument("prompt_file", help="Path to the prompt template file")
	parser.add_argument(
		"--assemble-only",
		action="store_true",
		help="Expand include directives and workflow overlays but do not render placeholders",
	)
	parser.add_argument(
		"--input-already-assembled",
		action="store_true",
		help="Treat the input file as already overlay-expanded and include-assembled",
	)
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


def _find_prompt_root(prompt_path: Path) -> Path | None:
	resolved_prompt_path = prompt_path.resolve()
	for candidate in (resolved_prompt_path.parent, *resolved_prompt_path.parents):
		if candidate.name == "prompts":
			return candidate
	return None


def _prompt_base_dir(prompt_path: Path) -> Path | None:
	prompt_root = _find_prompt_root(prompt_path)
	if prompt_root is None:
		return None
	return prompt_root.parent


def _unique_paths(paths: list[Path]) -> tuple[Path, ...]:
	ordered_paths: list[Path] = []
	seen_paths: set[Path] = set()
	for candidate in paths:
		if candidate in seen_paths:
			continue
		seen_paths.add(candidate)
		ordered_paths.append(candidate)
	return tuple(ordered_paths)


def _resolve_include_path(current_path: Path, include_target: str) -> Path:
	include_path = Path(include_target.strip())
	if not include_target.strip():
		raise PromptAssemblyError(f"Empty include target in '{current_path}'")
	if include_path.is_absolute():
		raise PromptAssemblyError(
			f"Absolute include paths are not allowed in '{current_path}': {include_target}"
		)

	prompt_root = _find_prompt_root(current_path)
	candidate_paths = [
		(current_path.parent / include_path).resolve(),
	]
	if prompt_root is not None:
		candidate_paths.append((prompt_root / include_path).resolve())

	for candidate in _unique_paths(candidate_paths):
		if prompt_root is not None:
			try:
				candidate.relative_to(prompt_root)
			except ValueError:
				continue
		if candidate.is_file():
			return candidate

	raise PromptAssemblyError(
		f"Included prompt fragment not found from '{current_path}': {include_target}"
	)


def _assemble_prompt_fragment(fragment: PromptFragment, *, stack: tuple[Path, ...]) -> str:
	assembled_lines: list[str] = []
	for raw_line in fragment.text.splitlines():
		include_match = INCLUDE_DIRECTIVE_PATTERN.fullmatch(raw_line)
		if include_match is None:
			assembled_lines.append(f"{raw_line}\n")
			continue

		include_path = _resolve_include_path(fragment.path, include_match.group(1))
		if include_path in stack:
			include_chain = " -> ".join(str(path) for path in (*stack, include_path))
			raise PromptAssemblyError(f"Cyclic prompt include detected: {include_chain}")
		assembled_lines.append(
			_assemble_prompt_fragment(
				PromptFragment(path=include_path, text=load_prompt(include_path)),
				stack=(*stack, include_path),
			)
		)
	return "".join(assembled_lines)


def assemble_prompt_fragments(fragments: tuple[PromptFragment, ...]) -> str:
	return "".join(
		_assemble_prompt_fragment(fragment, stack=(fragment.path.resolve(),))
		for fragment in fragments
	)


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
	prompt_root = _prompt_base_dir(prompt_path)

	_append_contract_candidate(candidates, seen, prompt_root, mode_name)
	_append_contract_candidate(candidates, seen, Path.cwd(), mode_name)
	_append_contract_candidate(candidates, seen, script_root, mode_name)
	_append_contract_candidate(candidates, seen, Path.cwd() / ".codex-workflow-src", mode_name)
	_append_contract_candidate(candidates, seen, Path.cwd() / ".codex-workflow-src-main", mode_name)

	for candidate in candidates:
		if candidate.is_file():
			return candidate
	return None


def _append_reference_candidate(
	candidates: list[Path],
	seen: set[Path],
	base_dir: Path | None,
	file_name: str,
) -> None:
	if base_dir is None:
		return
	candidate = (base_dir / "prompts" / "references" / file_name).resolve()
	if candidate in seen:
		return
	seen.add(candidate)
	candidates.append(candidate)


def discover_reference_path(prompt_path: Path, file_name: str) -> Path | None:
	candidates: list[Path] = []
	seen: set[Path] = set()
	script_root = Path(__file__).resolve().parents[1]
	prompt_root = _prompt_base_dir(prompt_path)

	_append_reference_candidate(candidates, seen, prompt_root, file_name)
	_append_reference_candidate(candidates, seen, Path.cwd(), file_name)
	_append_reference_candidate(candidates, seen, script_root, file_name)
	_append_reference_candidate(candidates, seen, Path.cwd() / ".codex-workflow-src", file_name)
	_append_reference_candidate(candidates, seen, Path.cwd() / ".codex-workflow-src-main", file_name)

	for candidate in candidates:
		if candidate.is_file():
			return candidate
	return None


def _expected_reference_path(prompt_path: Path, file_name: str) -> Path:
	prompt_root = _prompt_base_dir(prompt_path) or Path.cwd()
	return prompt_root / "prompts" / "references" / file_name


def discover_persona_source_path(prompt_path: Path | None) -> Path | None:
	candidates: list[Path] = []
	seen: set[Path] = set()
	script_root = Path(__file__).resolve().parents[1]
	prompt_root = _prompt_base_dir(prompt_path) if prompt_path is not None else None

	for base_dir in (
		prompt_root,
		Path.cwd(),
		script_root,
		Path.cwd() / ".codex-workflow-src",
		Path.cwd() / ".codex-workflow-src-main",
	):
		if base_dir is None:
			continue
		candidate = (base_dir / "prompts" / PERSONA_SOURCE_FILE_NAME).resolve()
		if candidate in seen:
			continue
		seen.add(candidate)
		candidates.append(candidate)

	for candidate in candidates:
		if candidate.is_file():
			return candidate
	return None


def load_phase_c_persona_prefixes(prompt_path: Path | None) -> dict[str, str]:
	persona_source_path = discover_persona_source_path(prompt_path)
	if persona_source_path is None:
		return {}
	try:
		payload = json.loads(persona_source_path.read_text(encoding="utf-8"))
	except OSError as exc:
		raise PromptLoadError(
			f"Unable to read persona source '{persona_source_path}': {exc}"
		) from exc
	except json.JSONDecodeError as exc:
		raise PromptLoadError(
			f"Invalid JSON persona source '{persona_source_path}' at line {exc.lineno}, column {exc.colno}: {exc.msg}"
		) from exc

	if not isinstance(payload, dict):
		raise PromptLoadError(
			f"Persona source '{persona_source_path}' must decode to an object mapping mode names to prefixes"
		)

	prefixes: dict[str, str] = {}
	for raw_mode_name, raw_prefix in payload.items():
		if not isinstance(raw_mode_name, str) or OVERLAY_MODE_NAME_PATTERN.fullmatch(raw_mode_name) is None:
			raise PromptLoadError(
				f"Persona source '{persona_source_path}' contains invalid mode name {raw_mode_name!r}"
			)
		if not isinstance(raw_prefix, str):
			raise PromptLoadError(
				f"Persona source '{persona_source_path}' must use string prefixes for mode '{raw_mode_name}'"
			)
		prefixes[raw_mode_name] = raw_prefix
	return prefixes


def _reference_file_name_for_placeholder(placeholder_name: str) -> str | None:
	if not placeholder_name.startswith("REFERENCE_"):
		return None
	reference_stem = placeholder_name.removeprefix("REFERENCE_").strip().lower()
	if not reference_stem:
		return None
	return reference_stem.replace("_", "-") + ".txt"


def _load_reference_placeholder_value(
	*,
	prompt_path: Path,
	mode_name: str,
	placeholder_name: str,
) -> str:
	file_name = _reference_file_name_for_placeholder(placeholder_name)
	if file_name is None:
		raise PromptLoadError(f"Unsupported reference placeholder '{placeholder_name}'")

	reference_path = discover_reference_path(prompt_path, file_name)
	if reference_path is None:
		expected_path = _expected_reference_path(prompt_path, file_name)
		raise PromptLoadError(
			f"Reference file for placeholder '{placeholder_name}' not found: {expected_path}"
		)

	reference_text = load_prompt(reference_path)

	mode_specific_append_name = None
	if placeholder_name == "REFERENCE_OUTPUT_CONTRACT" and mode_name == "mode-validate-generate":
		mode_specific_append_name = "validate-output-contract.txt"

	if mode_specific_append_name is None:
		return reference_text

	mode_specific_append_path = discover_reference_path(prompt_path, mode_specific_append_name)
	if mode_specific_append_path is None:
		expected_path = _expected_reference_path(prompt_path, mode_specific_append_name)
		raise PromptLoadError(
			f"Append reference file '{mode_specific_append_name}' for placeholder '{placeholder_name}' not found: {expected_path}"
		)

	return reference_text + load_prompt(mode_specific_append_path)


def hydrate_reference_placeholders(
	*,
	prompt_text: str,
	prompt_path: Path,
	mode_name: str,
	values: dict[str, str],
) -> dict[str, str]:
	hydrated = dict(values)
	for placeholder_name in collect_placeholders(prompt_text):
		if not placeholder_name.startswith("REFERENCE_"):
			continue
		if hydrated.get(placeholder_name, "") != "":
			continue
		hydrated[placeholder_name] = _load_reference_placeholder_value(
			prompt_path=prompt_path,
			mode_name=mode_name,
			placeholder_name=placeholder_name,
		)
	return hydrated


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
		elif isinstance(raw_default, bool):
			default_value = "true" if raw_default else "false"
		elif isinstance(raw_default, (int, float)):
			default_value = str(raw_default)
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


def _placeholder_expression_allowed_as_literal(line: str, match: re.Match[str]) -> bool:
	placeholder_expression = match.group(1)
	if match.start() > 0 and line[match.start() - 1] == "$":
		return True
	return LITERAL_DOT_EXPRESSION_PATTERN.fullmatch(placeholder_expression) is not None


def validate_supported_template_syntax(prompt_text: str, prompt_path: Path) -> None:
	violations: list[str] = []
	for line_number, line in enumerate(prompt_text.splitlines(), start=1):
		matches = list(PLACEHOLDER_EXPRESSION_PATTERN.finditer(line))
		last_end = 0
		residual_segments: list[str] = []

		for match in matches:
			residual_segments.append(line[last_end:match.start()])
			last_end = match.end()
			placeholder_expression = match.group(1)
			if VARIABLE_NAME_PATTERN.fullmatch(placeholder_expression) is not None:
				continue
			if _placeholder_expression_allowed_as_literal(line, match):
				continue
			violations.append(
				f"{prompt_path}:{line_number}: unsupported placeholder expression '{{{{{placeholder_expression}}}}}'"
			)

		residual_segments.append(line[last_end:])
		residual_text = "".join(residual_segments)
		if "{{" in residual_text:
			violations.append(
				f"{prompt_path}:{line_number}: unmatched placeholder delimiter in {line.strip()!r}"
			)
		if "{%" in line or "%}" in line:
			violations.append(
				f"{prompt_path}:{line_number}: unsupported template tag syntax {line.strip()!r}"
			)

	if violations:
		raise TemplateSyntaxError("Unsupported template syntax:\n" + "\n".join(violations))


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


def _workflow_edit_restriction_value() -> str:
	if os.environ.get("ALLOW_WORKFLOW_EDITS", "false") == "true":
		return "- CI workflow edits under .github/workflows/ are permitted when required by the approved plan; keep changes inside the plan's stated file scope."
	return "- Do not change CI workflows."


def _persona_prefix_enabled() -> bool:
	raw_value = os.environ.get("PROMPT_PERSONA_PREFIX_ENABLED", "true").strip().lower()
	return bool(raw_value) and raw_value not in FALSEY_ENV_VALUES


def apply_phase_c_persona_prefix(prompt_text: str, *, prompt_path: Path | None = None, mode_name: str) -> str:
	if not _persona_prefix_enabled():
		return prompt_text
	prefix = load_phase_c_persona_prefixes(prompt_path).get(mode_name)
	if prefix is None or prompt_text.startswith(prefix):
		return prompt_text
	return prefix + prompt_text


def collect_legacy_env_values() -> dict[str, str]:
	return {
		"WORKFLOW_EDIT_RESTRICTION": _workflow_edit_restriction_value(),
		"SEMBLE_PREFETCH": os.environ.get("SEMBLE_PREFETCH", ""),
		"SERENA_TOOL_HINTS": os.environ.get("SERENA_TOOL_HINTS", ""),
	}


def _coerce_overlay_mode_name(raw_value: Any, *, field_name: str) -> str:
	if not isinstance(raw_value, str):
		raise WorkflowOverlayError(f"Field '{field_name}' must be a string")
	mode_name = raw_value.strip()
	if not mode_name:
		raise WorkflowOverlayError(f"Field '{field_name}' must not be empty")
	if OVERLAY_MODE_NAME_PATTERN.fullmatch(mode_name) is None:
		raise WorkflowOverlayError(
			f"Field '{field_name}' contains invalid mode name '{mode_name}'"
		)
	return mode_name


def _coerce_overlay_path(raw_value: Any, *, field_name: str) -> str | None:
	if raw_value is None:
		return None
	if not isinstance(raw_value, str):
		raise WorkflowOverlayError(f"Field '{field_name}' must be a string when present")
	path_value = raw_value.strip()
	if not path_value:
		raise WorkflowOverlayError(f"Field '{field_name}' must not be empty when present")
	return path_value


def load_workflow_overlay_from_env() -> WorkflowOverlay | None:
	raw_payload = os.environ.get("WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON", "").strip()
	if not raw_payload:
		return None

	try:
		payload = json.loads(raw_payload)
	except json.JSONDecodeError as exc:
		raise WorkflowOverlayError(
			f"Invalid WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON payload: {exc.msg}"
		) from exc

	if not isinstance(payload, list):
		raise WorkflowOverlayError("WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON must decode to a list")

	repo_root_raw = os.environ.get("WORKFLOW_OVERLAY_REPO_ROOT", "").strip()
	if repo_root_raw:
		repo_root = Path(repo_root_raw).resolve()
		if not repo_root.exists():
			raise WorkflowOverlayError(
				f"WORKFLOW_OVERLAY_REPO_ROOT must point to an existing directory: {repo_root_raw}"
			)
		if not repo_root.is_dir():
			raise WorkflowOverlayError(
				f"WORKFLOW_OVERLAY_REPO_ROOT must point to a directory: {repo_root_raw}"
			)
	else:
		repo_root = Path.cwd().resolve()
	overrides: list[PromptOverride] = []
	for index, item in enumerate(payload):
		if not isinstance(item, dict):
			raise WorkflowOverlayError(
				f"Prompt override entry #{index + 1} must be an object"
			)
		unknown_keys = sorted(set(item.keys()) - {"mode", "append_path", "replace_path"})
		if unknown_keys:
			raise WorkflowOverlayError(
				f"Prompt override entry #{index + 1} contains unknown keys: {', '.join(unknown_keys)}"
			)
		mode_name = _coerce_overlay_mode_name(item.get("mode"), field_name=f"prompt_overrides[{index}].mode")
		append_path = _coerce_overlay_path(
			item.get("append_path"),
			field_name=f"prompt_overrides[{index}].append_path",
		)
		replace_path = _coerce_overlay_path(
			item.get("replace_path"),
			field_name=f"prompt_overrides[{index}].replace_path",
		)
		if (append_path is None) == (replace_path is None):
			raise WorkflowOverlayError(
				f"Prompt override entry #{index + 1} must set exactly one of append_path or replace_path"
			)
		overrides.append(
			PromptOverride(
				mode_name=mode_name,
				append_path=append_path,
				replace_path=replace_path,
			)
		)

	return WorkflowOverlay(repo_root=repo_root, overrides=tuple(overrides))


def _resolve_overlay_fragment_path(repo_root: Path, raw_path: str) -> Path:
	fragment_path = Path(raw_path)
	if not fragment_path.is_absolute():
		fragment_path = (repo_root / fragment_path).resolve()
	else:
		fragment_path = fragment_path.resolve()
	try:
		fragment_path.relative_to(repo_root)
	except ValueError as exc:
		raise WorkflowOverlayError(
			f"Overlay fragment path escapes repo root '{repo_root}': {raw_path}"
		) from exc
	if not fragment_path.is_file():
		raise WorkflowOverlayError(f"Overlay fragment not found: {fragment_path}")
	return fragment_path


def apply_workflow_overlay(
	prompt_path: Path,
	prompt_text: str,
	*,
	mode_name: str,
) -> tuple[PromptFragment, ...]:
	overlay = load_workflow_overlay_from_env()
	if overlay is None or not overlay.overrides:
		return (PromptFragment(path=prompt_path, text=prompt_text),)

	matches = [override for override in overlay.overrides if override.mode_name == mode_name]
	if not matches:
		return (PromptFragment(path=prompt_path, text=prompt_text),)
	if len(matches) > 1:
		raise WorkflowOverlayError(
			f"Multiple workflow-overlay prompt overrides matched mode '{mode_name}'"
		)

	match = matches[0]
	if match.replace_path is not None:
		replacement_path = _resolve_overlay_fragment_path(overlay.repo_root, match.replace_path)
		return (PromptFragment(path=replacement_path, text=load_prompt(replacement_path)),)

	if match.append_path is None:
		raise WorkflowOverlayError(
			f"Workflow-overlay prompt override for mode '{mode_name}' is missing append/replace path"
		)
	append_path = _resolve_overlay_fragment_path(overlay.repo_root, match.append_path)
	return (
		PromptFragment(path=prompt_path, text=prompt_text),
		PromptFragment(path=append_path, text=load_prompt(append_path)),
	)


def _render_inline_placeholders(line: str, values: dict[str, str]) -> str:
	def replace_match(match: re.Match[str]) -> str:
		placeholder_name = match.group(1)
		if placeholder_name in LEGACY_STANDALONE_ONLY_VARS:
			return match.group(0)
		return values.get(placeholder_name, match.group(0))

	return PLACEHOLDER_PATTERN.sub(replace_match, line)


def render_prompt_text(prompt_text: str, values: dict[str, str]) -> str:
	rendered_lines: list[str] = []
	for line in prompt_text.splitlines():
		match = STANDALONE_PLACEHOLDER_PATTERN.fullmatch(line)
		if match is not None:
			placeholder_name = match.group(1)
			if placeholder_name in values:
				rendered_lines.append(_render_replacement(values[placeholder_name]))
				continue
		rendered_lines.append(f"{_render_inline_placeholders(line, values)}\n")
	return "".join(rendered_lines)


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	prompt_path = Path(args.prompt_file)

	try:
		mode_name = resolve_mode_name(prompt_path, args.legacy_mode_name)
		if args.input_already_assembled:
			prompt_text = load_prompt(prompt_path)
		else:
			prompt_fragments = apply_workflow_overlay(
				prompt_path,
				load_prompt(prompt_path),
				mode_name=mode_name,
			)
			prompt_text = assemble_prompt_fragments(prompt_fragments)
		validate_supported_template_syntax(prompt_text, prompt_path)
		if args.assemble_only:
			sys.stdout.write(prompt_text)
			return 0
		provided_values = parse_cli_variables(args.variables)
		legacy_env_values = collect_legacy_env_values()
		contract_path = discover_contract_path(prompt_path, mode_name)
		if contract_path is not None:
			contract = load_contract(contract_path, mode_name)
			effective_values = dict(contract.optional_vars)
			effective_values.update(legacy_env_values)
			effective_values.update(provided_values)
			validate_contract(contract, prompt_text, effective_values)
		else:
			effective_values = dict(legacy_env_values)
			effective_values.update(provided_values)
		effective_values = hydrate_reference_placeholders(
			prompt_text=prompt_text,
			prompt_path=prompt_path,
			mode_name=mode_name,
			values=effective_values,
		)
		prompt_text = apply_phase_c_persona_prefix(prompt_text, prompt_path=prompt_path, mode_name=mode_name)
		sys.stdout.write(render_prompt_text(prompt_text, effective_values))
	except RenderPromptError as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 1

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
