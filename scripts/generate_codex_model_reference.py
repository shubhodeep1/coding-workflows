#!/usr/bin/env python3
"""Generate docs/codex-model-reference.md from the Codex model catalog."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "scripts" / "codex_model_catalog.json"
OVERRIDES_PATH = REPO_ROOT / "scripts" / "codex_model_catalog_overrides.yaml"
OUTPUT_PATH = REPO_ROOT / "docs" / "codex-model-reference.md"
GENERATED_BANNER = (
	"<!-- GENERATED FILE: do not edit. Run `make generate` after editing "
	"scripts/codex_model_catalog.json. -->"
)


def fail(message: str) -> None:
	print(f"::error::FAIL: {message}", file=sys.stderr)
	raise SystemExit(1)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Generate the Codex model reference markdown document."
	)
	mode_group = parser.add_mutually_exclusive_group(required=True)
	mode_group.add_argument("--write", action="store_true", help="Write the generated document")
	mode_group.add_argument(
		"--check",
		action="store_true",
		help="Check the committed document against freshly generated output",
	)
	return parser.parse_args()


def load_catalog(path: Path) -> list[dict[str, object]]:
	try:
		with path.open("r", encoding="utf-8") as handle:
			payload = json.load(handle)
	except FileNotFoundError:
		fail(f"{path.as_posix()}: file not found")
	except json.JSONDecodeError as exc:
		fail(f"{path.as_posix()}: invalid JSON: {exc}")
	except OSError as exc:
		fail(f"{path.as_posix()}: {exc}")
	if not isinstance(payload, dict):
		fail(f"{path.as_posix()}: expected top-level object")
	models = payload.get("models")
	if not isinstance(models, list):
		fail(f"{path.as_posix()}: expected top-level 'models' list")
	return models


def load_optional_overrides(path: Path) -> dict[str, dict[str, object]]:
	if not path.exists():
		return {}

	try:
		import yaml
	except ModuleNotFoundError:
		fail("PyYAML is required when scripts/codex_model_catalog_overrides.yaml is present")

	try:
		with path.open("r", encoding="utf-8") as handle:
			payload = yaml.safe_load(handle)
	except yaml.YAMLError as exc:
		fail(f"{path.as_posix()}: invalid YAML: {exc}")
	except OSError as exc:
		fail(f"{path.as_posix()}: {exc}")

	if payload is None:
		return {}
	if not isinstance(payload, dict):
		fail(f"{path.as_posix()}: expected top-level mapping")

	rows = payload.get("models", [])
	if not isinstance(rows, list):
		fail(f"{path.as_posix()}: expected 'models' to be a list")

	overrides_by_slug: dict[str, dict[str, object]] = {}
	for index, row in enumerate(rows, start=1):
		if not isinstance(row, dict):
			fail(f"{path.as_posix()}: models[{index}] must be a mapping")

		slug = row.get("slug")
		if not isinstance(slug, str) or not slug:
			fail(f"{path.as_posix()}: models[{index}].slug must be a non-empty string")
		if slug in overrides_by_slug:
			fail(f"{path.as_posix()}: duplicate override entry for slug {slug}")

		row_overrides = row.get("overrides")
		if row_overrides is None:
			row_overrides = {}
		if not isinstance(row_overrides, dict):
			fail(f"{path.as_posix()}: models[{index}].overrides must be a mapping")

		notes = row.get("notes", "")
		if notes is None:
			notes = ""
		elif not isinstance(notes, str):
			notes = str(notes)

		overrides_by_slug[slug] = {
			"overrides": dict(row_overrides),
			"notes": notes,
		}

	return overrides_by_slug


def format_scalar(value: object) -> str:
	if value is None:
		return "null"
	if isinstance(value, bool):
		return "true" if value else "false"
	return str(value)


def sanitize_table_cell(text: str) -> str:
	collapsed = " ".join(text.split())
	return collapsed.replace("|", r"\|")


def render_notes(override_entry: dict[str, object]) -> str:
	raw_notes = override_entry.get("notes", "")
	notes = sanitize_table_cell(raw_notes if isinstance(raw_notes, str) else str(raw_notes))
	override_values = override_entry.get("overrides")
	has_pinned_fields = isinstance(override_values, dict) and bool(override_values)

	if has_pinned_fields and "(frozen)" not in notes:
		notes = f"{notes} (frozen)" if notes else "(frozen)"

	return notes or "—"


def merge_model(base_model: dict[str, object], override_entry: dict[str, object] | None) -> dict[str, object]:
	merged_model = dict(base_model)
	if override_entry is None:
		return merged_model

	override_values = override_entry.get("overrides")
	if isinstance(override_values, dict):
		merged_model.update(override_values)
	return merged_model


def render_markdown(
	models: list[dict[str, object]], overrides_by_slug: dict[str, dict[str, object]]
) -> str:
	known_slugs: list[str] = []
	lines = [
		GENERATED_BANNER,
		"",
		"# Codex model reference",
		"",
		"This file is generated from `scripts/codex_model_catalog.json` and optional overrides in `scripts/codex_model_catalog_overrides.yaml`.",
		"Rows with pinned override fields are marked `(frozen)` in `notes`.",
		"",
		"| slug | default_verbosity | support_verbosity | apply_patch_tool_type | notes |",
		"| --- | --- | --- | --- | --- |",
	]

	for index, model in enumerate(models, start=1):
		if not isinstance(model, dict):
			fail(f"{CATALOG_PATH.as_posix()}: models[{index}] must be a mapping")

		slug = model.get("slug")
		if not isinstance(slug, str) or not slug:
			fail(f"{CATALOG_PATH.as_posix()}: models[{index}].slug must be a non-empty string")
		if slug in known_slugs:
			fail(f"{CATALOG_PATH.as_posix()}: duplicate catalog entry for slug {slug}")

		known_slugs.append(slug)
		override_entry = overrides_by_slug.get(slug)
		merged_model = merge_model(model, override_entry)
		row = [
			sanitize_table_cell(format_scalar(slug)),
			sanitize_table_cell(format_scalar(merged_model.get("default_verbosity"))),
			sanitize_table_cell(format_scalar(merged_model.get("support_verbosity"))),
			sanitize_table_cell(format_scalar(merged_model.get("apply_patch_tool_type"))),
			render_notes(override_entry or {}),
		]
		lines.append(f"| {' | '.join(row)} |")

	unknown_override_slugs = sorted(set(overrides_by_slug) - set(known_slugs))
	if unknown_override_slugs:
		fail(
			"scripts/codex_model_catalog_overrides.yaml: unknown slug(s): "
			+ ", ".join(unknown_override_slugs)
		)

	return "\n".join(lines) + "\n"


def write_output(path: Path, rendered_text: str) -> int:
	try:
		path.write_text(rendered_text, encoding="utf-8")
	except OSError as exc:
		fail(f"{path.as_posix()}: unable to write generated output: {exc}")
	return 0


def check_output(path: Path, rendered_text: str) -> int:
	rendered_bytes = rendered_text.encode("utf-8")
	try:
		existing_bytes = path.read_bytes() if path.exists() else b""
	except OSError as exc:
		fail(f"{path.as_posix()}: unable to read existing output: {exc}")
	if existing_bytes == rendered_bytes:
		return 0

	existing_text = existing_bytes.decode("utf-8", errors="replace")
	diff_text = "".join(
		difflib.unified_diff(
			existing_text.splitlines(keepends=True),
			rendered_text.splitlines(keepends=True),
			fromfile=path.as_posix(),
			tofile=f"{path.as_posix()} (generated)",
		)
	)
	if diff_text:
		sys.stderr.write(diff_text if diff_text.endswith("\n") else diff_text + "\n")
	print(f"::error::FAIL: {path.as_posix()} is out of date; run make generate", file=sys.stderr)
	return 1


def main() -> int:
	args = parse_args()
	models = load_catalog(CATALOG_PATH)
	overrides_by_slug = load_optional_overrides(OVERRIDES_PATH)
	rendered_text = render_markdown(models, overrides_by_slug)
	if args.write:
		return write_output(OUTPUT_PATH, rendered_text)
	return check_output(OUTPUT_PATH, rendered_text)


if __name__ == "__main__":
	raise SystemExit(main())
