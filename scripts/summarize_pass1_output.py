#!/usr/bin/env python3
"""Compress one pass-1 reviewer output into a compact findings ledger.

Invoked as a background process from scripts/review_run_reviewers.sh the
moment a pass-1 reviewer writes its success status. Running the summariser
in parallel with the remaining pass-1 reviewers removes summarisation wall
time from the critical path before pass-2 starts.

On OpenRouter failure (HTTP error, timeout, empty content) the script
writes a fallback that head-truncates the source to --fallback-lines and
appends one tab-separated row to --failure-registry so the main shell can
emit a single aggregated Telegram alert at end of pass-1. Exit code is
non-zero ONLY for catastrophic errors; a graceful fallback still returns 0
so the calling shell does not treat a TG-reported degradation as a hard
failure.

The --done-sentinel file is always written (success, failed, or fallback)
so the waiter loop in review_run_reviewers.sh can count completions
without special-casing missing files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


def _build_messages(source_reviewer: str, target_lines: int, pass1_output: str) -> list[dict]:
	system = (
		"You compress one pass-1 code review output into a compact findings ledger "
		"for downstream reviewer (pass-2) and editor models. This is STRICT "
		"summarisation — do NOT invent, weaken, strengthen, or drop findings."
	)
	user = (
		f"Source reviewer: {source_reviewer}\n\n"
		f"OUTPUT FORMAT (max {target_lines} lines, sentinel-delimited):\n\n"
		f"=== FINDINGS FROM {source_reviewer} ===\n"
		"- {file}:{line_range} | severity={low|medium|high|critical} | confidence=[1-5]\n"
		"  PROBLEM: one-sentence statement of the bug\n"
		"  WHY: one-sentence justification or quoted evidence from the source\n"
		"- ...\n"
		f"=== END FINDINGS FROM {source_reviewer} ===\n\n"
		"Rules:\n"
		"1. Preserve EVERY distinct finding. If space forces a choice, prefer\n"
		"   high-severity / high-confidence; NEVER drop silently — collapse related\n"
		"   items into a single entry with \"(N related items)\" suffix.\n"
		"2. Keep file paths and line numbers verbatim. Do not guess.\n"
		"3. Do not invent severities or confidence scores. If the source did not\n"
		"   state one, omit that field rather than fabricate.\n"
		"4. No prose. No preambles. No markdown headers other than the === sentinels.\n"
		f"5. If source reported no findings, output:\n"
		f"   === FINDINGS FROM {source_reviewer} ===\n"
		"   (No findings reported.)\n"
		f"   === END FINDINGS FROM {source_reviewer} ===\n\n"
		f"Stay strictly within {target_lines} lines. Count before replying; compress\n"
		"again if over.\n\n"
		"--- BEGIN SOURCE PASS-1 OUTPUT ---\n"
		f"{pass1_output}\n"
		"--- END SOURCE PASS-1 OUTPUT ---\n"
	)
	return [
		{"role": "system", "content": system},
		{"role": "user", "content": user},
	]


def _call_openrouter(
	*,
	api_key: str,
	base_url: str,
	model: str,
	messages: list[dict],
	thinking_level: str,
	max_tokens: int,
	timeout_secs: int,
) -> str:
	payload: dict = {
		"model": model,
		"messages": messages,
		"temperature": 0.0,
		"max_tokens": max_tokens,
	}
	if thinking_level:
		payload["reasoning"] = thinking_level

	request = urllib.request.Request(
		f"{base_url.rstrip('/')}/chat/completions",
		data=json.dumps(payload).encode("utf-8"),
		headers={
			"Content-Type": "application/json",
			"Authorization": f"Bearer {api_key}",
		},
		method="POST",
	)

	try:
		with urllib.request.urlopen(request, timeout=timeout_secs) as response:
			response_text = response.read().decode("utf-8")
	except urllib.error.HTTPError as exc:
		try:
			error_body = exc.read().decode("utf-8", errors="replace")
		except Exception:  # noqa: BLE001
			error_body = ""
		raise RuntimeError(
			f"OpenRouter HTTP {exc.code}: {error_body[:400] or exc.reason}"
		) from exc
	except (urllib.error.URLError, OSError, TimeoutError) as exc:
		raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

	try:
		response_payload = json.loads(response_text)
	except json.JSONDecodeError as exc:
		raise RuntimeError(f"OpenRouter returned invalid JSON: {exc}") from exc

	if not isinstance(response_payload, dict):
		raise RuntimeError("OpenRouter returned non-object JSON response")

	choices = response_payload.get("choices")
	if not isinstance(choices, list) or not choices:
		raise RuntimeError("OpenRouter response did not include choices")

	first = choices[0] if isinstance(choices[0], dict) else {}
	message = first.get("message") if isinstance(first.get("message"), dict) else {}
	content = message.get("content")

	# OpenRouter may return content as string OR as a list of content parts.
	if isinstance(content, list):
		parts = []
		for part in content:
			if isinstance(part, dict):
				text = part.get("text")
				if isinstance(text, str):
					parts.append(text)
		text_out = "".join(parts)
	elif isinstance(content, str):
		text_out = content
	else:
		text_out = ""

	if not text_out.strip():
		raise RuntimeError("OpenRouter response content was empty")

	return text_out.rstrip() + "\n"


def _write_fallback(
	input_path: Path,
	output_path: Path,
	source_reviewer: str,
	fallback_lines: int,
) -> None:
	try:
		raw_lines = input_path.read_text(encoding="utf-8", errors="replace").splitlines()
	except OSError:
		raw_lines = []
	truncated = raw_lines[:fallback_lines]
	body = "\n".join(
		[
			f"=== FINDINGS FROM {source_reviewer} (TRUNCATED FALLBACK — summariser unavailable) ===",
			*truncated,
			f"=== END FINDINGS FROM {source_reviewer} ===",
		]
	)
	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(body + "\n", encoding="utf-8")


def _record_failure(registry_path: Path, source_reviewer: str, reason: str) -> None:
	try:
		registry_path.parent.mkdir(parents=True, exist_ok=True)
		sanitised = reason.replace("\n", " ").replace("\t", " ")[:240]
		with registry_path.open("a", encoding="utf-8") as fh:
			fh.write(f"{source_reviewer}\t{sanitised}\n")
	except OSError:
		pass


def _write_sentinel(sentinel_path: Path, status: str) -> None:
	try:
		sentinel_path.parent.mkdir(parents=True, exist_ok=True)
		sentinel_path.write_text(status, encoding="utf-8")
	except OSError:
		pass


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--input", required=True, help="Path to the pass-1 reviewer output file.")
	parser.add_argument("--output", required=True, help="Path to write the compressed summary.")
	parser.add_argument("--done-sentinel", required=True, help="Path to write status sentinel.")
	parser.add_argument("--failure-registry", required=True, help="TSV file appended on failure.")
	parser.add_argument("--source-reviewer", required=True, help="Slug of the pass-1 reviewer model.")
	parser.add_argument("--model", required=True, help="Summariser model slug (e.g. openai/gpt-5.4-mini).")
	parser.add_argument("--target-lines", type=int, default=160)
	parser.add_argument("--reasoning-effort", default="xhigh")
	parser.add_argument("--call-timeout-secs", type=int, default=180)
	parser.add_argument(
		"--max-input-lines",
		type=int,
		default=3000,
		help="Pre-truncate source input above this line count to stay within model context.",
	)
	parser.add_argument("--fallback-lines", type=int, default=200)
	args = parser.parse_args()

	input_path = Path(args.input)
	output_path = Path(args.output)
	sentinel_path = Path(args.done_sentinel)
	registry_path = Path(args.failure_registry)

	try:
		if not input_path.is_file():
			raise RuntimeError(f"input file missing: {input_path}")

		try:
			raw = input_path.read_text(encoding="utf-8", errors="replace")
		except OSError as exc:
			raise RuntimeError(f"could not read input file: {exc}") from exc

		raw_lines = raw.splitlines()
		original_line_count = len(raw_lines)
		if original_line_count > args.max_input_lines:
			raw_lines = raw_lines[: args.max_input_lines]
			raw = "\n".join(raw_lines)
			print(
				f"INFO: pre-truncated {args.source_reviewer} input "
				f"from {original_line_count} to {args.max_input_lines} lines.",
				file=sys.stderr,
			)

		api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
		if not api_key:
			raise RuntimeError("OPENROUTER_API_KEY env var not set")

		base_url = os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_DEFAULT_BASE_URL)

		messages = _build_messages(args.source_reviewer, args.target_lines, raw)

		# Rough max_tokens budget: ~120 tokens per target output line covers the
		# ledger format comfortably (avg line ~40–80 chars plus thinking budget).
		max_tokens = max(1024, args.target_lines * 120)

		summary = _call_openrouter(
			api_key=api_key,
			base_url=base_url,
			model=args.model,
			messages=messages,
			thinking_level=args.reasoning_effort,
			max_tokens=max_tokens,
			timeout_secs=args.call_timeout_secs,
		)

		output_path.parent.mkdir(parents=True, exist_ok=True)
		output_path.write_text(summary, encoding="utf-8")
		_write_sentinel(sentinel_path, "success")
		return 0

	except Exception as exc:
		reason = str(exc)
		print(
			f"ERROR: summariser failed for {args.source_reviewer}: {reason}",
			file=sys.stderr,
		)
		try:
			_write_fallback(
				input_path,
				output_path,
				args.source_reviewer,
				args.fallback_lines,
			)
		except Exception as fallback_exc:  # noqa: BLE001
			print(
				f"ERROR: fallback truncation also failed: {fallback_exc}",
				file=sys.stderr,
			)
		_record_failure(registry_path, args.source_reviewer, reason)
		_write_sentinel(sentinel_path, "failed")
		# Graceful degradation: pass-2 still gets a usable (truncated) summary,
		# so do not surface a non-zero exit to the parent shell.
		return 0


if __name__ == "__main__":
	sys.exit(main())
