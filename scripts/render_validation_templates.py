#!/usr/bin/env python3
"""Render validation harness templates from a slot manifest."""

from __future__ import annotations

import argparse
import json
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
			output_rel = template_path.relative_to(family_dir)
			output_rel = output_rel.with_suffix("")
			_ensure_safe_relative_path(output_rel, what="Rendered output path")
			output_rel = _resolve_output_rel_path(family, output_rel)
			template_map[output_rel.as_posix()] = TemplateSpec(
				template_rel_path=template_rel_path.as_posix(),
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
		if target.suffix == ".sh":
			try:
				target.chmod(target.stat().st_mode | 0o111)
			except OSError as exc:
				raise OutputWriteError(f"Failed setting executable bits on '{target}': {exc}") from exc
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
		template_specs = collect_templates(templates_root, family)
		context = build_render_context(manifest, family)
		context.setdefault("output_root_name", output_root.name or "validation")
		rendered_files = render_templates(template_specs, templates_root, context)
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
