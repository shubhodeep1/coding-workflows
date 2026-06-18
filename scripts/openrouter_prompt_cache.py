#!/usr/bin/env python3
"""OpenRouter prompt-cache helpers shared by workflow scripts."""

from __future__ import annotations

import os
from typing import Any


OPENROUTER_PROMPT_BUDGET_TOKENS_DEFAULT = 160000


def parse_bool(value: Any, default: bool = False) -> bool:
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	text = str(value).strip().lower()
	if text in {"1", "true", "yes", "on", "y"}:
		return True
	if text in {"0", "false", "no", "off", "n"}:
		return False
	return default


def is_cache_disabled(value: str | None = None) -> bool:
	if value is None:
		value = os.getenv("OPENROUTER_PROMPT_CACHE_DISABLED", "false")
	return parse_bool(value, default=False)


def is_gemini_model(model_id: str | None) -> bool:
	text = str(model_id or "").strip().lower()
	if not text:
		return False
	return "gemini" in text


def should_add_explicit_breakpoint(model_id: str | None, cache_disabled: bool | None = None) -> bool:
	disabled = is_cache_disabled() if cache_disabled is None else bool(cache_disabled)
	return (not disabled) and (not is_gemini_model(model_id))


def add_ephemeral_cache_breakpoint(
	messages: list[dict[str, Any]],
	*,
	model_id: str | None,
	cache_disabled: bool | None = None,
) -> tuple[list[dict[str, Any]], bool]:
	"""Add an ephemeral cache breakpoint to the first message when allowed."""
	copied = [dict(message) for message in messages]
	if not copied:
		return copied, False
	if not should_add_explicit_breakpoint(model_id, cache_disabled):
		return copied, False
	first = dict(copied[0])
	first["cache_control"] = {"type": "ephemeral"}
	copied[0] = first
	return copied, True


def _to_int_or_none(value: Any) -> int | None:
	if value is None:
		return None
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def _nested_int(source: dict[str, Any], *path: str) -> int | None:
	current: Any = source
	for key in path:
		if not isinstance(current, dict):
			return None
		current = current.get(key)
	return _to_int_or_none(current)


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, int | None]:
	usage_data = usage if isinstance(usage, dict) else {}
	cache_creation = _to_int_or_none(usage_data.get("cache_creation_input_tokens"))
	if cache_creation is None:
		cache_creation = _nested_int(usage_data, "prompt_tokens_details", "cache_write_tokens")
	if cache_creation is None:
		cache_creation = _nested_int(usage_data, "input_token_details", "cache_creation")

	cache_read = _to_int_or_none(usage_data.get("cache_read_input_tokens"))
	if cache_read is None:
		cache_read = _nested_int(usage_data, "prompt_tokens_details", "cached_tokens")
	if cache_read is None:
		cache_read = _nested_int(usage_data, "input_token_details", "cache_read")

	return {
		"prompt_tokens": _to_int_or_none(usage_data.get("prompt_tokens")),
		"completion_tokens": _to_int_or_none(usage_data.get("completion_tokens")),
		"total_tokens": _to_int_or_none(usage_data.get("total_tokens")),
		"cache_creation_input_tokens": cache_creation,
		"cache_read_input_tokens": cache_read,
	}


def should_retry_without_breakpoint(http_code: int, error_text: str) -> bool:
	if http_code not in {400, 422}:
		return False
	text = (error_text or "").lower()
	return "cache_control" in text or "cache control" in text or "ephemeral" in text


def format_usage_value(value: int | None) -> str:
	if value is None:
		return "na"
	return str(value)


def compact_if_over_budget(
	sections: list[tuple[int, str, str]],
	budget_tokens: int | None,
) -> list[tuple[int, str, str]]:
	"""Return retained ordered ``(tier, label, body)`` sections within the prompt budget.

	The input is an ordered list of ``(tier, label, body)`` tuples where ``tier=1`` is
	the highest-priority keep-always floor and larger tier numbers are lower-priority
	sections dropped first. Each ``body`` should already include any separators or
	newlines the caller wants counted toward the final prompt size; ``label`` is
	preserved for caller bookkeeping only. When multiple sections share a tier, later
	sections are dropped before earlier sections so earlier context survives longest.
	The return value preserves the original tuple shape and the original relative order
	of any retained sections. When ``budget_tokens`` is ``None`` or invalid, the helper
	falls back to ``OPENROUTER_PROMPT_BUDGET_TOKENS`` and then to the default budget of
	160000 tokens. Budgeting uses the repo's shared approximation of ``~4 chars/token``.
	"""
	copied_sections = [tuple(section) for section in sections]
	resolved_budget_tokens = _to_int_or_none(budget_tokens)
	if resolved_budget_tokens is None:
		resolved_budget_tokens = _to_int_or_none(os.getenv("OPENROUTER_PROMPT_BUDGET_TOKENS"))
	if resolved_budget_tokens is None:
		resolved_budget_tokens = OPENROUTER_PROMPT_BUDGET_TOKENS_DEFAULT
	if resolved_budget_tokens < 0:
		resolved_budget_tokens = 0
	if not copied_sections:
		return copied_sections
	total_chars = sum(len(body) for _, _, body in copied_sections)
	if (total_chars + 3) // 4 <= resolved_budget_tokens:
		return copied_sections

	dropped_indexes: set[int] = set()
	drop_candidate_indexes = [
		index
		for index, section in sorted(
			enumerate(copied_sections),
			key=lambda item: (item[1][0], item[0]),
			reverse=True,
		)
		if section[0] > 1
	]
	for dropped_index in drop_candidate_indexes:
		dropped_indexes.add(dropped_index)
		total_chars -= len(copied_sections[dropped_index][2])
		if (total_chars + 3) // 4 <= resolved_budget_tokens:
			break
	return [
		section
		for index, section in enumerate(copied_sections)
		if index not in dropped_indexes
	]
