#!/usr/bin/env python3
"""CS ticket investigation helpers for ``scripts/cs_investigate.sh`` (issue #9440).

Stdlib only; run as ``python3 -I -B``. Every input that came from the CS hub,
the agent DB bridge or the model is untrusted data: it is parsed as JSON or
bytes only, never executed, and enters the prompt only inside UNTRUSTED
blocks with every line prefixed ``UNTRUSTED_DATA:``.

Subcommands:

  * ``validate --schema S --ticket-id ID --mode M --reply FILE`` — find the
    JSON object in the model reply, check it against the cs_verdict v1 schema
    (``scripts/templates/cs_verdict.v1.schema.json``), check ``ticket_id`` and
    ``mode`` equal the inputs and the compact form is at most 32,768 bytes.
    Prints the compact ASCII verdict (exit 0), or one reason token
    (``not_json``, ``schema_invalid``, ``ticket_mismatch``,
    ``outcome_mode_mismatch``, ``too_large``, ``schema_unsupported``) and exits 1.
  * ``classify --reply FILE --requests-dir DIR`` — ``requests <n>`` when the
    reply is exactly ``{"bridge_requests":[...]}`` (1 to 5 objects, each at
    most 4,096 bytes compact; written to ``DIR/req-<i>.json``), ``verdict``
    for any other JSON object, or ``invalid reason=<token>``.
  * ``compose ...`` — build one round's prompt (see ``cmd_compose``).
  * ``invalid --ticket-id ID`` — the exact two-key ``invalid_verdict`` object.
  * ``image-ext FILE`` — ``jpg``, ``png``, ``gif`` or ``webp`` from the magic
    bytes, or exit 1.

Exit codes: 0 success; 1 the input fails the check; 2 invalid arguments.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

TICKET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MODES = ("investigate", "verify")
# Outcomes only a verify run may return; the other six are investigate-only.
VERIFY_OUTCOMES = ("verified_fixed", "verify_failed")
MAX_VERDICT_BYTES = 32768
MAX_REQUESTS = 5
MAX_REQUEST_BYTES = 4096
MAX_TICKET_PROMPT_BYTES = 65536
MAX_UNTRUSTED_PROMPT_BYTES = 163840
MAX_REPLY_BYTES = 1048576
UNTRUSTED_PREFIX = "UNTRUSTED_DATA: "

# Keywords the stdlib validator understands. Annotation keywords are ignored;
# any other keyword in the schema fails closed (D5).
VALIDATION_KEYWORDS = frozenset(
	(
		"type",
		"required",
		"properties",
		"additionalProperties",
		"enum",
		"minLength",
		"maxLength",
		"minItems",
		"maxItems",
		"minimum",
		"maximum",
		"items",
		"pattern",
	)
)
ANNOTATION_KEYWORDS = frozenset(("$schema", "$id", "$comment", "title", "description"))


class SchemaUnsupported(ValueError):
	"""The schema uses a keyword the stdlib validator does not implement."""


# --- JSON reading ------------------------------------------------------------


def _reject_constant(value: str) -> Any:
	raise ValueError(f"non-finite number {value}")


def _loads(text: str) -> Any:
	return json.loads(text, parse_constant=_reject_constant)


def extract_object(text: str) -> dict[str, Any] | None:
	"""The top-level JSON object of a model reply, or ``None``.

	Accepts the bare object (surrounding whitespace stripped) or the object
	inside one code fence. Only a top-level JSON object is accepted; arrays,
	scalars and prose around the object are not.
	"""
	stripped = (text or "").strip()
	candidates = [stripped]
	fence = re.fullmatch(r"```[A-Za-z0-9_-]*\s*\n(.*?)\n?```", stripped, re.S)
	if fence:
		candidates.append(fence.group(1).strip())
	for candidate in candidates:
		try:
			value = _loads(candidate)
		except ValueError:
			continue
		if isinstance(value, dict):
			return value
	return None


def compact(value: Any) -> str:
	return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _read_reply(path: str) -> str:
	try:
		data = Path(path).read_bytes()[:MAX_REPLY_BYTES]
	except OSError:
		return ""
	return data.decode("utf-8", errors="replace")


# --- stdlib schema validator -------------------------------------------------


def _type_ok(value: Any, name: str) -> bool:
	if name == "object":
		return isinstance(value, dict)
	if name == "array":
		return isinstance(value, list)
	if name == "string":
		return isinstance(value, str)
	if name == "integer":
		return isinstance(value, int) and not isinstance(value, bool)
	if name == "number":
		return (isinstance(value, (int, float)) and not isinstance(value, bool)) and math.isfinite(value)
	if name == "boolean":
		return isinstance(value, bool)
	if name == "null":
		return value is None
	raise SchemaUnsupported(f"type {name!r}")


def validate_schema(value: Any, schema: Any, path: str = "$") -> list[str]:
	"""Errors of ``value`` against ``schema``; empty when valid.

	Raises ``SchemaUnsupported`` for any keyword outside VALIDATION_KEYWORDS
	and ANNOTATION_KEYWORDS, so a schema change the validator cannot honour
	fails closed instead of passing everything.
	"""
	if not isinstance(schema, dict):
		raise SchemaUnsupported(f"schema at {path} is not an object")
	unknown = set(schema) - VALIDATION_KEYWORDS - ANNOTATION_KEYWORDS
	if unknown:
		raise SchemaUnsupported(f"keyword(s) {sorted(unknown)} at {path}")
	errors: list[str] = []
	if "type" in schema:
		types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
		if not any(_type_ok(value, name) for name in types):
			return [f"{path}: expected type {types}"]
	if "enum" in schema and value not in schema["enum"]:
		errors.append(f"{path}: not in enum")
	if isinstance(value, str):
		if "minLength" in schema and len(value) < schema["minLength"]:
			errors.append(f"{path}: shorter than {schema['minLength']}")
		if "maxLength" in schema and len(value) > schema["maxLength"]:
			errors.append(f"{path}: longer than {schema['maxLength']}")
		if "pattern" in schema and not re.search(schema["pattern"], value):
			errors.append(f"{path}: does not match pattern")
	if isinstance(value, (int, float)) and not isinstance(value, bool):
		if "minimum" in schema and value < schema["minimum"]:
			errors.append(f"{path}: below minimum")
		if "maximum" in schema and value > schema["maximum"]:
			errors.append(f"{path}: above maximum")
	if isinstance(value, list):
		if "minItems" in schema and len(value) < schema["minItems"]:
			errors.append(f"{path}: fewer than {schema['minItems']} items")
		if "maxItems" in schema and len(value) > schema["maxItems"]:
			errors.append(f"{path}: more than {schema['maxItems']} items")
		if "items" in schema:
			for index, item in enumerate(value):
				errors += validate_schema(item, schema["items"], f"{path}[{index}]")
	if isinstance(value, dict):
		properties = schema.get("properties", {})
		for key in schema.get("required", []):
			if key not in value:
				errors.append(f"{path}: missing {key}")
		additional = schema.get("additionalProperties", True)
		for key, item in value.items():
			if key in properties:
				errors += validate_schema(item, properties[key], f"{path}.{key}")
			elif additional is False:
				errors.append(f"{path}: unexpected property")
			elif isinstance(additional, dict):
				errors += validate_schema(item, additional, f"{path}.*")
	return errors


def validate_verdict(reply: str, schema: Any, ticket_id: str, mode: str) -> tuple[str | None, str]:
	"""(compact verdict, "") when valid, else (None, reason token)."""
	verdict = extract_object(reply)
	if verdict is None:
		return None, "not_json"
	try:
		errors = validate_schema(verdict, schema)
	except SchemaUnsupported:
		return None, "schema_unsupported"
	if errors:
		return None, "schema_invalid"
	if verdict.get("ticket_id") != ticket_id or verdict.get("mode") != mode:
		return None, "ticket_mismatch"
	if (verdict.get("outcome") in VERIFY_OUTCOMES) != (mode == "verify"):
		return None, "outcome_mode_mismatch"
	text = compact(verdict)
	if len(text.encode("ascii")) > MAX_VERDICT_BYTES:
		return None, "too_large"
	return text, ""


def classify_reply(reply: str) -> tuple[str, list[dict[str, Any]]]:
	"""("requests", requests) | ("verdict", []) | ("invalid reason=<token>", [])."""
	value = extract_object(reply)
	if value is None:
		return "invalid reason=not_json", []
	if "bridge_requests" not in value:
		return "verdict", []
	requests = value.get("bridge_requests")
	if set(value) != {"bridge_requests"} or not isinstance(requests, list):
		return "invalid reason=bad_requests", []
	if not 1 <= len(requests) <= MAX_REQUESTS:
		return "invalid reason=request_count", []
	for request in requests:
		if not isinstance(request, dict) or len(compact(request).encode("ascii")) > MAX_REQUEST_BYTES:
			return "invalid reason=bad_requests", []
	return "requests", requests


def image_ext(data: bytes) -> str | None:
	if data.startswith(b"\xff\xd8\xff"):
		return "jpg"
	if data.startswith(b"\x89PNG\r\n\x1a\n"):
		return "png"
	if data.startswith((b"GIF87a", b"GIF89a")):
		return "gif"
	if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
		return "webp"
	return None


# --- prompt composition ------------------------------------------------------


def untrusted_block(label: str, text: str) -> str:
	"""Wrap ``text`` so no line of it can pose as a trusted marker."""
	lines = text.splitlines() or [""]
	body = "\n".join(UNTRUSTED_PREFIX + line for line in lines)
	return f"BEGIN UNTRUSTED {label}\n{body}\nEND UNTRUSTED {label}\n"


def _cap_text(data: bytes, limit: int) -> tuple[str, bool]:
	truncated = len(data) > limit
	return data[:limit].decode("utf-8", errors="replace"), truncated


def _ticket_text(path: Path) -> str:
	try:
		data = path.read_bytes()
	except OSError:
		return "(ticket unavailable)"
	try:
		data = json.dumps(_loads(data.decode("utf-8")), indent=2, ensure_ascii=False).encode("utf-8")
	except (UnicodeDecodeError, ValueError):
		pass
	text, truncated = _cap_text(data, MAX_TICKET_PROMPT_BYTES)
	if truncated:
		text += "\n(ticket truncated by the workflow at 64 KB)"
	return text


def _round_results(results_dir: Path | None) -> list[dict[str, str]]:
	"""Every bridge result, oldest first: ``round-<r>/req-<i>.{json,rc,body}``."""
	if results_dir is None or not results_dir.is_dir():
		return []

	def number(path: Path) -> int:
		match = re.search(r"(\d+)$", path.stem if path.is_file() else path.name)
		return int(match.group(1)) if match else 0

	results: list[dict[str, str]] = []
	for round_dir in sorted((p for p in results_dir.iterdir() if p.is_dir() and re.fullmatch(r"round-\d+", p.name)), key=number):
		for request in sorted(round_dir.glob("req-*.json"), key=number):
			stem = request.with_suffix("")
			try:
				request_text = request.read_text(encoding="utf-8", errors="replace")
			except OSError:
				request_text = ""
			try:
				rc = Path(f"{stem}.rc").read_text(encoding="utf-8").strip()
			except OSError:
				rc = "unknown"
			try:
				body = Path(f"{stem}.body").read_bytes()
			except OSError:
				body = b""
			body_text, truncated = _cap_text(body, 16384)
			if truncated:
				body_text += "\n(response truncated by the workflow at 16 KB)"
			results.append({
				"label": f"BRIDGE RESULT {round_dir.name} {request.stem}",
				"request": request_text,
				"rc": rc if re.fullmatch(r"\d{1,3}", rc) else "unknown",
				"body": body_text,
			})
	return results


def compose_prompt(
	template: str,
	ticket_id: str,
	mode: str,
	attempt: str,
	round_number: int,
	max_rounds: int,
	ticket_file: Path,
	photos: list[str],
	notes: list[str],
	results_dir: Path | None,
) -> str:
	accept = round_number < max_rounds
	header = [
		"## Run context (trusted, written by the workflow)",
		"",
		f"- ticket_id: {ticket_id}",
		f"- mode: {mode}",
		f"- attempt: {attempt}",
		f"- round: {round_number} of at most {max_rounds} bridge query rounds",
	]
	if accept:
		header.append(
			f"- bridge requests: accepted this round ({max_rounds - round_number} round(s) left, at most {MAX_REQUESTS} requests per round)"
		)
	else:
		header.append("- bridge requests: NO LONGER ACCEPTED. Reply with the final cs_verdict object now.")
	header.append("- photos: " + (", ".join(photos) if photos else "none"))
	header.append("- workflow notes: " + (", ".join(notes) if notes else "none"))
	parts = [template.rstrip() + "\n", "\n".join(header) + "\n"]

	ticket_text = _ticket_text(ticket_file)
	ticket_block = untrusted_block("TICKET", ticket_text)
	# The raw ticket cap is applied before the per-line prefix, so a ticket of
	# many short lines can grow past the untrusted budget and crowd out every
	# bridge result. Re-cap on whole lines (the block keeps its END marker).
	ticket_cap = MAX_UNTRUSTED_PROMPT_BYTES // 2
	if len(ticket_block.encode("utf-8")) > ticket_cap:
		kept: list[str] = []
		size = 256
		for line in ticket_text.splitlines():
			size += len((UNTRUSTED_PREFIX + line + "\n").encode("utf-8"))
			if size > ticket_cap:
				break
			kept.append(line)
		ticket_block = untrusted_block("TICKET", "\n".join(kept + ["(ticket truncated by the workflow to fit the prompt cap)"]))
	results = _round_results(results_dir)
	result_blocks: list[str] = []
	for result in results:
		text = (
			f"request: {result['request'].strip()}\n"
			f"cs_bridge_query exit code: {result['rc']} (0 = HTTP 2xx, 4 = HTTP error, 2 = refused, 3 = no OIDC token)\n"
			f"response:\n{result['body']}"
		)
		result_blocks.append(untrusted_block(result["label"], text))
	# Drop the oldest results first until the untrusted data fits.
	dropped = 0
	while result_blocks and len((ticket_block + "".join(result_blocks)).encode("utf-8")) > MAX_UNTRUSTED_PROMPT_BYTES:
		result_blocks.pop(0)
		dropped += 1
	if dropped:
		parts.append(f"(The workflow dropped the {dropped} oldest bridge result(s) to fit the prompt cap.)\n")
	parts.append(ticket_block)
	parts.extend(result_blocks)
	text = "\n".join(parts)
	encoded = text.encode("utf-8")
	hard_cap = MAX_UNTRUSTED_PROMPT_BYTES + len(parts[0].encode("utf-8")) + 8192
	if len(encoded) > hard_cap:
		text = encoded[:hard_cap].decode("utf-8", errors="ignore") + "\n(prompt truncated by the workflow)\n"
	return text


# --- command line ------------------------------------------------------------


def _check_ticket_mode(ticket_id: str, mode: str) -> None:
	if not TICKET_ID_RE.match(ticket_id) or mode not in MODES:
		raise SystemExit(2)


def cmd_validate(args: argparse.Namespace) -> int:
	_check_ticket_mode(args.ticket_id, args.mode)
	try:
		schema = _loads(Path(args.schema).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		print("schema_unsupported")
		return 1
	verdict, reason = validate_verdict(_read_reply(args.reply), schema, args.ticket_id, args.mode)
	if verdict is None:
		print(reason)
		return 1
	print(verdict)
	return 0


def cmd_classify(args: argparse.Namespace) -> int:
	kind, requests = classify_reply(_read_reply(args.reply))
	if kind != "requests":
		print(kind)
		return 0
	out = Path(args.requests_dir)
	out.mkdir(parents=True, exist_ok=True)
	for index, request in enumerate(requests):
		(out / f"req-{index}.json").write_text(compact(request) + "\n", encoding="ascii")
	print(f"requests {len(requests)}")
	return 0


def cmd_compose(args: argparse.Namespace) -> int:
	_check_ticket_mode(args.ticket_id, args.mode)
	if not re.fullmatch(r"[1-9]", args.attempt):
		raise SystemExit(2)
	template = Path(args.template).read_text(encoding="utf-8")
	notes = [note for note in (args.note or []) if re.fullmatch(r"[a-z0-9_]{1,60}", note)]
	photos = [photo for photo in (args.photo or []) if re.fullmatch(r"/[A-Za-z0-9_./-]{1,400}", photo)]
	text = compose_prompt(
		template,
		args.ticket_id,
		args.mode,
		args.attempt,
		args.round,
		args.max_rounds,
		Path(args.ticket_file),
		photos,
		notes,
		Path(args.results_dir) if args.results_dir else None,
	)
	Path(args.out).write_text(text, encoding="utf-8")
	return 0


def cmd_invalid(args: argparse.Namespace) -> int:
	if not TICKET_ID_RE.match(args.ticket_id):
		raise SystemExit(2)
	print(compact({"ticket_id": args.ticket_id, "outcome": "invalid_verdict"}))
	return 0


def cmd_image_ext(args: argparse.Namespace) -> int:
	try:
		with open(args.file, "rb") as handle:
			head = handle.read(16)
	except OSError:
		return 1
	ext = image_ext(head)
	if ext is None:
		return 1
	print(ext)
	return 0


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)

	p = sub.add_parser("validate")
	p.add_argument("--schema", required=True)
	p.add_argument("--ticket-id", required=True)
	p.add_argument("--mode", required=True)
	p.add_argument("--reply", required=True)
	p.set_defaults(func=cmd_validate)

	p = sub.add_parser("classify")
	p.add_argument("--reply", required=True)
	p.add_argument("--requests-dir", required=True)
	p.set_defaults(func=cmd_classify)

	p = sub.add_parser("compose")
	p.add_argument("--template", required=True)
	p.add_argument("--ticket-id", required=True)
	p.add_argument("--mode", required=True)
	p.add_argument("--attempt", required=True)
	p.add_argument("--round", type=int, required=True)
	p.add_argument("--max-rounds", type=int, default=3)
	p.add_argument("--ticket-file", required=True)
	p.add_argument("--results-dir", default="")
	p.add_argument("--photo", action="append")
	p.add_argument("--note", action="append")
	p.add_argument("--out", required=True)
	p.set_defaults(func=cmd_compose)

	p = sub.add_parser("invalid")
	p.add_argument("--ticket-id", required=True)
	p.set_defaults(func=cmd_invalid)

	p = sub.add_parser("image-ext")
	p.add_argument("file")
	p.set_defaults(func=cmd_image_ext)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	return args.func(args)


if __name__ == "__main__":
	sys.exit(main())
