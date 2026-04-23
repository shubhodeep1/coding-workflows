#!/usr/bin/env python3
"""Render validation harness templates from a slot manifest."""

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
except ImportError:  # pragma: no cover - exercised via runtime dependency checks
	yaml = None

try:
	from jsonschema import Draft202012Validator
	from jsonschema.exceptions import SchemaError
except ImportError:  # pragma: no cover - exercised via runtime dependency checks
	Draft202012Validator = None
	SchemaError = None

try:
	from jinja2 import Environment, FileSystemLoader, StrictUndefined
	from jinja2.exceptions import TemplateError
except ImportError:  # pragma: no cover - exercised via runtime dependency checks
	Environment = None
	FileSystemLoader = None
	StrictUndefined = None
	TemplateError = Exception


class RenderValidationTemplatesError(Exception):
	"""Base error class for manifest rendering failures."""


class DependencyError(RenderValidationTemplatesError):
	"""Raised when a required runtime dependency is missing."""


class ManifestLoadError(RenderValidationTemplatesError):
	"""Raised when manifest loading fails."""


class SchemaLoadError(RenderValidationTemplatesError):
	"""Raised when schema loading fails."""


class ManifestValidationError(RenderValidationTemplatesError):
	"""Raised when manifest validation fails."""


class FamilyResolutionError(RenderValidationTemplatesError):
	"""Raised when manifest family routing fails."""


class TemplateCollectionError(RenderValidationTemplatesError):
	"""Raised when collecting templates fails."""


class TemplateRenderError(RenderValidationTemplatesError):
	"""Raised when rendering templates fails."""


class OutputWriteError(RenderValidationTemplatesError):
	"""Raised when output write operations fail."""


@dataclass(frozen=True)
class FamilySpec:
	"""Template family routing metadata."""

	name: str
	relative_dir: str


@dataclass(frozen=True)
class TemplateSpec:
	"""Template source and destination mapping."""

	template_rel_path: str
	output_rel_path: Path


@dataclass(frozen=True)
class RenderedFile:
	"""Rendered output payload."""

	output_rel_path: Path
	content: str


FAMILY_REGISTRY: dict[str, FamilySpec] = {
	"python-mongo-flask": FamilySpec(name="python-mongo-flask", relative_dir="python-mongo-flask"),
	"node-hardhat-solidity": FamilySpec(name="node-hardhat-solidity", relative_dir="node-hardhat-solidity"),
}

RENDERED_OUTPUT_ALIASES: dict[str, dict[str, str]] = {
	"python-mongo-flask": {
		"tests/10_http_smoke.sh": "tests/11_http_smoke.sh",
	},
	"node-hardhat-solidity": {
		"tests/20_rpc_probe.sh": "tests/25_rpc_probe.sh",
	},
}

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_SCHEMA_BYTES = 2 * 1024 * 1024

PYTHON_MONGO_FLASK_TEST_OUTPUTS: dict[str, str] = {
	"canary": "tests/00_canary.sh",
	"family_marker": "tests/10_family_marker.sh",
	"http_smoke": "tests/11_http_smoke.sh",
	"import_audit": "tests/20_import_audit.sh",
	"graceful_shutdown": "tests/30_graceful_shutdown.sh",
}
PYTHON_MONGO_FLASK_TEST_ORDER: tuple[str, ...] = (
	"canary",
	"family_marker",
	"http_smoke",
	"import_audit",
	"graceful_shutdown",
)
PYTHON_MONGO_FLASK_ALWAYS_TEST_OUTPUTS: tuple[str, ...] = ("tests/90_tap_report.sh",)
PYTHON_MONGO_FLASK_CANARY_ID = "canary"
PYTHON_MONGO_FLASK_CUSTOM_TEST_START_PREFIX = 40
PYTHON_MONGO_FLASK_CUSTOM_TEST_MAX_PREFIX = 89
PYTHON_MONGO_FLASK_SELECTION_METADATA_PATH = "_meta/test_selection.json"
EXTERNAL_TOOL_IDS: tuple[str, ...] = ("forge", "cast", "hardhat")
TOOL_TOKEN_CHARS = r"A-Za-z0-9._+-"


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Render validation harness templates from .ai/validate.yml")
	parser.add_argument(
		"--manifest",
		default=".ai/validate.yml",
		help="Path to slot manifest YAML (default: .ai/validate.yml)",
	)
	parser.add_argument(
		"--schema",
		default="scripts/templates/slot_manifest.schema.json",
		help="Path to manifest JSON schema",
	)
	parser.add_argument(
		"--templates-root",
		default="workflow-templates/validation-harness",
		help="Root directory containing validation harness templates",
	)
	parser.add_argument(
		"--output-root",
		default="validation",
		help="Output root where rendered files are written",
	)
	return parser


def _missing_dependency_error(package_name: str) -> DependencyError:
	return DependencyError(
		f"Missing dependency '{package_name}'. Install required packages with: "
		"pip install pyyaml jsonschema jinja2"
	)


def _normalize_newlines(content: str) -> str:
	normalized = content.replace("\r\n", "\n").replace("\r", "\n")
	if not normalized.endswith("\n"):
		normalized += "\n"
	return normalized


def _stable_value(value: Any) -> Any:
	if isinstance(value, dict):
		return {key: _stable_value(value[key]) for key in sorted(value.keys(), key=str)}
	if isinstance(value, list):
		return [_stable_value(item) for item in value]
	return value


def _json_pointer_from_path(path_parts: list[Any]) -> str:
	if not path_parts:
		return "$"
	segments: list[str] = []
	for segment in path_parts:
		escaped = str(segment).replace("~", "~0").replace("/", "~1")
		segments.append(escaped)
	return "/" + "/".join(segments)


def _normalize_manifest_string_list(manifest: dict[str, Any], key: str) -> list[str]:
	raw_value = manifest.get(key)
	if raw_value is None:
		return []
	if not isinstance(raw_value, list):
		raise ManifestValidationError(f"Manifest key '{key}' must be an array of strings")

	normalized: list[str] = []
	for idx, item in enumerate(raw_value, start=1):
		if not isinstance(item, str):
			raise ManifestValidationError(
				f"Manifest key '{key}' item #{idx} must be a string, got {type(item).__name__}"
			)
		value = item.strip()
		if not value:
			raise ManifestValidationError(f"Manifest key '{key}' item #{idx} must not be empty")
		normalized.append(value)
	return normalized


def _find_external_tools_in_command(command: str) -> list[str]:
	required: list[str] = []
	for tool in EXTERNAL_TOOL_IDS:
		pattern = re.compile(rf"(?<![{TOOL_TOKEN_CHARS}]){re.escape(tool)}(?![{TOOL_TOKEN_CHARS}])", re.IGNORECASE)
		if pattern.search(command):
			required.append(tool)
	return required


def _python_mongo_flask_selection_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
	raw_skip_tests = _normalize_manifest_string_list(manifest, "skip_tests")
	skip_test_ids: list[str] = []
	seen_skip_ids: set[str] = set()

	for raw_id in raw_skip_tests:
		normalized_id = raw_id.strip().lower()
		if normalized_id not in PYTHON_MONGO_FLASK_TEST_OUTPUTS:
			supported = ", ".join(PYTHON_MONGO_FLASK_TEST_ORDER)
			raise ManifestValidationError(
				f"Manifest key 'skip_tests' contains unsupported test id '{raw_id}'. Supported ids: {supported}"
			)
		if normalized_id in seen_skip_ids:
			raise ManifestValidationError(
				f"Manifest key 'skip_tests' contains duplicate test id '{raw_id}'"
			)
		if normalized_id == PYTHON_MONGO_FLASK_CANARY_ID:
			raise ManifestValidationError(
				"Manifest key 'skip_tests' cannot skip test id 'canary' because tests/00_canary.sh is mandatory"
			)
		seen_skip_ids.add(normalized_id)
		skip_test_ids.append(normalized_id)

	selected_test_ids = [
		test_id for test_id in PYTHON_MONGO_FLASK_TEST_ORDER if test_id not in seen_skip_ids
	]
	selected_static_test_outputs = [
		PYTHON_MONGO_FLASK_TEST_OUTPUTS[test_id] for test_id in selected_test_ids
	]

	custom_test_commands = _normalize_manifest_string_list(manifest, "custom_tests")
	custom_capacity = PYTHON_MONGO_FLASK_CUSTOM_TEST_MAX_PREFIX - PYTHON_MONGO_FLASK_CUSTOM_TEST_START_PREFIX + 1
	if len(custom_test_commands) > custom_capacity:
		raise ManifestValidationError(
			f"Manifest key 'custom_tests' supports at most {custom_capacity} commands for python-mongo-flask"
		)

	custom_tests: list[dict[str, Any]] = []
	for idx, command in enumerate(custom_test_commands, start=1):
		prefix = PYTHON_MONGO_FLASK_CUSTOM_TEST_START_PREFIX + idx - 1
		output_rel_path = f"tests/{prefix:02d}_custom_{idx:02d}.sh"
		custom_tests.append(
			{
				"index": idx,
				"output_rel_path": output_rel_path,
				"command": command,
				"required_tools": _find_external_tools_in_command(command),
			}
		)

	selected_test_outputs = [*selected_static_test_outputs]
	selected_test_outputs.extend(custom_test["output_rel_path"] for custom_test in custom_tests)
	selected_test_outputs.extend(PYTHON_MONGO_FLASK_ALWAYS_TEST_OUTPUTS)

	http_runtime_test_ids = {"http_smoke", "graceful_shutdown"}
	runtime_http_tests_enabled = any(test_id in http_runtime_test_ids for test_id in selected_test_ids)

	return {
		"family": "python-mongo-flask",
		"skip_test_ids": skip_test_ids,
		"selected_test_ids": selected_test_ids,
		"selected_static_test_outputs": selected_static_test_outputs,
		"selected_test_outputs": selected_test_outputs,
		"custom_tests": custom_tests,
		"runtime_http_tests_enabled": runtime_http_tests_enabled,
	}


def _render_custom_test_wrapper(command: str, index: int) -> str:
	escaped_command = command.replace("'", "'\"'\"'")
	label = f"custom validation command {index:02d}"
	return (
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
		"SCRIPT_DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
		"source \"${SCRIPT_DIR}/../_lib/tap_helpers.sh\"\n\n"
		f"CUSTOM_TEST_COMMAND='{escaped_command}'\n\n"
		"echo \"1..1\"\n\n"
		"set +e\n"
		"/bin/sh -c \"${CUSTOM_TEST_COMMAND}\"\n"
		"custom_rc=$?\n"
		"set -e\n\n"
		"if [ \"${custom_rc}\" -ne 0 ]; then\n"
		f"\ttap_not_ok 1 \"{label}\"\n"
		"\techo \"# custom test command failed: ${CUSTOM_TEST_COMMAND}\"\n"
		"\texit 1\n"
		"fi\n\n"
		f"tap_ok 1 \"{label}\"\n"
	)


def _build_family_selection(manifest: dict[str, Any], family: FamilySpec) -> dict[str, Any] | None:
	if family.name == "python-mongo-flask":
		return _python_mongo_flask_selection_from_manifest(manifest)
	return None


def _filter_template_specs_for_selection(
	template_specs: list[TemplateSpec],
	family: FamilySpec,
	family_selection: dict[str, Any] | None,
) -> list[TemplateSpec]:
	if family.name != "python-mongo-flask" or family_selection is None:
		return template_specs

	selected_static_outputs = set(
		str(path) for path in family_selection.get("selected_static_test_outputs", [])
	)
	selected_static_outputs.update(PYTHON_MONGO_FLASK_ALWAYS_TEST_OUTPUTS)
	known_static_outputs = set(PYTHON_MONGO_FLASK_TEST_OUTPUTS.values())
	known_static_outputs.update(PYTHON_MONGO_FLASK_ALWAYS_TEST_OUTPUTS)

	filtered: list[TemplateSpec] = []
	for template_spec in template_specs:
		output_rel = template_spec.output_rel_path.as_posix()
		if output_rel in known_static_outputs and output_rel not in selected_static_outputs:
			continue
		filtered.append(template_spec)
	return filtered


def _augment_rendered_files_for_selection(
	rendered_files: list[RenderedFile],
	family: FamilySpec,
	family_selection: dict[str, Any] | None,
) -> list[RenderedFile]:
	if family.name != "python-mongo-flask" or family_selection is None:
		return rendered_files

	augmented = [*rendered_files]
	custom_tests = family_selection.get("custom_tests", [])
	for custom_test in custom_tests:
		output_rel_path = Path(custom_test["output_rel_path"])
		_ensure_safe_relative_path(output_rel_path, what="Generated custom test path")
		augmented.append(
			RenderedFile(
				output_rel_path=output_rel_path,
				content=_normalize_newlines(
					_render_custom_test_wrapper(custom_test["command"], int(custom_test["index"]))
				),
			)
		)

	selection_metadata = {
		"schema_version": 1,
		"family": family_selection.get("family", family.name),
		"skip_test_ids": family_selection.get("skip_test_ids", []),
		"selected_test_ids": family_selection.get("selected_test_ids", []),
		"selected_test_outputs": family_selection.get("selected_test_outputs", []),
		"runtime_http_tests_enabled": bool(family_selection.get("runtime_http_tests_enabled", True)),
		"custom_tests": custom_tests,
	}
	metadata_path = Path(PYTHON_MONGO_FLASK_SELECTION_METADATA_PATH)
	_ensure_safe_relative_path(metadata_path, what="Selection metadata path")
	augmented.append(
		RenderedFile(
			output_rel_path=metadata_path,
			content=_normalize_newlines(json.dumps(selection_metadata, sort_keys=True, indent=2)),
		)
	)

	return augmented


def load_manifest(manifest_path: Path) -> dict[str, Any]:
	if yaml is None:
		raise _missing_dependency_error("PyYAML")
	if not manifest_path.exists():
		raise ManifestLoadError(f"Manifest file not found: {manifest_path}")
	try:
		manifest_size = manifest_path.stat().st_size
	except OSError as exc:
		raise ManifestLoadError(f"Unable to read manifest '{manifest_path}': {exc}") from exc
	if manifest_size > MAX_MANIFEST_BYTES:
		raise ManifestLoadError(
			f"Manifest file '{manifest_path}' is too large ({manifest_size} bytes > {MAX_MANIFEST_BYTES} bytes)"
		)
	try:
		manifest_raw = manifest_path.read_text(encoding="utf-8")
	except OSError as exc:
		raise ManifestLoadError(f"Unable to read manifest '{manifest_path}': {exc}") from exc

	try:
		manifest = yaml.safe_load(manifest_raw)
	except yaml.YAMLError as exc:
		location = ""
		mark = getattr(exc, "problem_mark", None)
		if mark is not None:
			location = f" at line {mark.line + 1}, column {mark.column + 1}"
		raise ManifestLoadError(f"Failed to parse manifest '{manifest_path}'{location}: {exc}") from exc

	if manifest is None:
		manifest = {}
	if not isinstance(manifest, dict):
		raise ManifestLoadError(
			f"Manifest root must be a mapping/object, got {type(manifest).__name__} in '{manifest_path}'"
		)
	return manifest


def load_schema(schema_path: Path) -> dict[str, Any]:
	if not schema_path.exists():
		raise SchemaLoadError(f"Schema file not found: {schema_path}")
	try:
		schema_size = schema_path.stat().st_size
	except OSError as exc:
		raise SchemaLoadError(f"Unable to read schema '{schema_path}': {exc}") from exc
	if schema_size > MAX_SCHEMA_BYTES:
		raise SchemaLoadError(
			f"Schema file '{schema_path}' is too large ({schema_size} bytes > {MAX_SCHEMA_BYTES} bytes)"
		)
	try:
		schema = json.loads(schema_path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as exc:
		raise SchemaLoadError(
			f"Invalid schema JSON in '{schema_path}' at line {exc.lineno}, column {exc.colno}: {exc.msg}"
		) from exc
	except OSError as exc:
		raise SchemaLoadError(f"Unable to read schema '{schema_path}': {exc}") from exc

	if not isinstance(schema, dict):
		raise SchemaLoadError(f"Schema root must be a JSON object in '{schema_path}'")
	return schema


def validate_manifest(manifest: dict[str, Any], schema: dict[str, Any]) -> None:
	if Draft202012Validator is None or SchemaError is None:
		raise _missing_dependency_error("jsonschema")
	try:
		Draft202012Validator.check_schema(schema)
		validator = Draft202012Validator(schema)
	except SchemaError as exc:
		raise SchemaLoadError(f"Invalid schema definition: {exc}") from exc
	errors = sorted(
		validator.iter_errors(manifest),
		key=lambda err: [str(part) for part in err.absolute_path],
	)
	if not errors:
		return

	detail_lines: list[str] = []
	for error in errors[:10]:
		path_pointer = _json_pointer_from_path(list(error.absolute_path))
		detail_lines.append(f"{path_pointer}: {error.message}")
	if len(errors) > 10:
		detail_lines.append(f"... {len(errors) - 10} additional validation errors")

	formatted = "\n".join(f"- {line}" for line in detail_lines)
	raise ManifestValidationError(f"Manifest validation failed:\n{formatted}")


def resolve_family(manifest: dict[str, Any]) -> FamilySpec:
	manifest_type = manifest.get("type")
	if not isinstance(manifest_type, str) or not manifest_type.strip():
		raise FamilyResolutionError("Manifest key 'type' must be a non-empty string")
	manifest_type = manifest_type.strip()
	family = FAMILY_REGISTRY.get(manifest_type)
	if family is None:
		supported = ", ".join(sorted(FAMILY_REGISTRY.keys(), key=str))
		raise FamilyResolutionError(
			f"Unknown manifest type '{manifest_type}'. Supported types: {supported}"
		)
	return family


def _ensure_safe_relative_path(path: Path, *, what: str) -> None:
	if path.is_absolute():
		raise TemplateCollectionError(f"{what} must be relative, got absolute path '{path}'")
	if any(part == ".." for part in path.parts):
		raise TemplateCollectionError(f"{what} must not contain '..': '{path}'")


def _resolve_output_rel_path(family: FamilySpec, output_rel: Path) -> Path:
	family_aliases = RENDERED_OUTPUT_ALIASES.get(family.name, {})
	remapped = family_aliases.get(output_rel.as_posix())
	if remapped is None:
		return output_rel
	remapped_path = Path(remapped)
	_ensure_safe_relative_path(remapped_path, what="Rendered output alias path")
	return remapped_path


def collect_templates(templates_root: Path, family: FamilySpec) -> list[TemplateSpec]:
	if not templates_root.exists() or not templates_root.is_dir():
		raise TemplateCollectionError(f"Templates root is missing or not a directory: {templates_root}")

	template_dir_order = ["_shared", family.relative_dir]
	if family.relative_dir == "_shared":
		template_dir_order = ["_shared"]

	template_map: dict[str, TemplateSpec] = {}
	for relative_dir in template_dir_order:
		family_dir = templates_root / relative_dir
		if not family_dir.exists():
			if relative_dir == "_shared":
				raise TemplateCollectionError(f"Shared template directory not found: {family_dir}")
			continue
		if not family_dir.is_dir():
			raise TemplateCollectionError(f"Template family path is not a directory: {family_dir}")

		for template_path in sorted(path for path in family_dir.rglob("*.j2") if path.is_file()):
			template_rel_path = template_path.relative_to(templates_root)
			template_rel = template_rel_path.as_posix()
			output_rel = template_path.relative_to(family_dir)
			output_rel = output_rel.with_suffix("")
			_ensure_safe_relative_path(output_rel, what="Rendered output path")
			output_rel = _resolve_output_rel_path(family, output_rel)
			output_key = output_rel.as_posix()
			existing_template = template_map.get(output_key)
			if existing_template is not None:
				existing_is_shared = existing_template.template_rel_path.startswith("_shared/")
				current_is_shared = template_rel.startswith("_shared/")
				if not (existing_is_shared and not current_is_shared):
					raise TemplateCollectionError(
						f"Duplicate rendered output path '{output_key}' from template '{template_rel}' (conflicts with '{existing_template.template_rel_path}')"
					)
			template_map[output_key] = TemplateSpec(
				template_rel_path=template_rel,
				output_rel_path=output_rel,
			)

	template_specs = [template_map[key] for key in sorted(template_map.keys(), key=str)]
	if not template_specs:
		raise TemplateCollectionError(
			f"No templates found for family '{family.name}' under {templates_root}"
		)
	return template_specs


def build_render_context(manifest: dict[str, Any], family: FamilySpec) -> dict[str, Any]:
	stable_manifest = _stable_value(manifest)
	slots = stable_manifest.get("slots")
	if not isinstance(slots, dict):
		slots = {}
	context: dict[str, Any] = {
		"family": family.name,
		"manifest": stable_manifest,
		"manifest_type": family.name,
		"slots": slots,
	}
	for key, value in stable_manifest.items():
		if key not in context:
			context[key] = value
	return context


def render_templates(
	template_specs: list[TemplateSpec],
	templates_root: Path,
	context: dict[str, Any],
) -> list[RenderedFile]:
	if Environment is None or FileSystemLoader is None or StrictUndefined is None:
		raise _missing_dependency_error("jinja2")

	environment = Environment(
		loader=FileSystemLoader(str(templates_root)),
		autoescape=False,
		keep_trailing_newline=True,
		undefined=StrictUndefined,
	)

	rendered_files: list[RenderedFile] = []
	for template_spec in template_specs:
		try:
			template = environment.get_template(template_spec.template_rel_path)
			rendered_content = template.render(**context)
		except TemplateError as exc:
			raise TemplateRenderError(
				f"Failed to render template '{template_spec.template_rel_path}': {exc}"
			) from exc
		rendered_files.append(
			RenderedFile(
				output_rel_path=template_spec.output_rel_path,
				content=_normalize_newlines(rendered_content),
			)
		)

	return rendered_files


def _ensure_path_within_root(output_root: Path, candidate: Path) -> None:
	try:
		candidate.relative_to(output_root)
	except ValueError as exc:
		raise OutputWriteError(
			f"Refusing to write outside output root '{output_root}': '{candidate}'"
		) from exc


def write_outputs(output_root: Path, rendered_files: list[RenderedFile]) -> list[Path]:
	try:
		output_root.mkdir(parents=True, exist_ok=True)
	except OSError as exc:
		raise OutputWriteError(f"Failed creating output root '{output_root}': {exc}") from exc
	resolved_root = output_root.resolve()
	written_paths: list[Path] = []

	for rendered_file in sorted(rendered_files, key=lambda item: item.output_rel_path.as_posix()):
		target = (output_root / rendered_file.output_rel_path).resolve()
		_ensure_path_within_root(resolved_root, target)
		try:
			target.parent.mkdir(parents=True, exist_ok=True)
		except OSError as exc:
			raise OutputWriteError(
				f"Failed creating parent directory for '{target}': {exc}"
			) from exc
		try:
			with open(target, "w", encoding="utf-8", newline="") as handle:
				handle.write(rendered_file.content)
		except OSError as exc:
			raise OutputWriteError(f"Failed writing rendered file '{target}': {exc}") from exc
		if target.suffix == ".sh" and "_lib" not in rendered_file.output_rel_path.parts:
			try:
				target.chmod(target.stat().st_mode | 0o111)
			except OSError as exc:
				print(f"::warning::Failed setting executable bits on '{target}': {exc}")
		written_paths.append(target)

	return written_paths


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)

	manifest_path = Path(args.manifest)
	schema_path = Path(args.schema)
	templates_root = Path(args.templates_root)
	output_root = Path(args.output_root)

	try:
		manifest = load_manifest(manifest_path)
		schema = load_schema(schema_path)
		validate_manifest(manifest, schema)
		family = resolve_family(manifest)
		family_selection = _build_family_selection(manifest, family)
		template_specs = collect_templates(templates_root, family)
		template_specs = _filter_template_specs_for_selection(template_specs, family, family_selection)
		context = build_render_context(manifest, family)
		if family_selection is not None:
			context["selection"] = _stable_value(family_selection)
			for key, value in family_selection.items():
				context.setdefault(key, value)
		context.setdefault("output_root_name", output_root.name or "validation")
		rendered_files = render_templates(template_specs, templates_root, context)
		rendered_files = _augment_rendered_files_for_selection(rendered_files, family, family_selection)
		written_paths = write_outputs(output_root, rendered_files)
	except RenderValidationTemplatesError as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 1

	print(
		f"Rendered {len(written_paths)} file(s) for family '{family.name}' into '{output_root}'.",
		flush=True,
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
