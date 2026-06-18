#!/usr/bin/env python3
"""OpenRouter prompt-cache helpers shared by workflow scripts."""

from __future__ import annotations

import os
from typing import Any


OPENROUTER_PROMPT_BUDGET_TOKENS_DEFAULT = 160000
CHARS_PER_TOKEN_ESTIMATE = 4
PromptSection = tuple[int, str, str]


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


def _resolve_budget_tokens(budget_tokens: int | None) -> int:
	resolved_budget_tokens = _to_int_or_none(budget_tokens)
	if resolved_budget_tokens is None:
		resolved_budget_tokens = _to_int_or_none(os.getenv("OPENROUTER_PROMPT_BUDGET_TOKENS"))
	if resolved_budget_tokens is None:
		resolved_budget_tokens = OPENROUTER_PROMPT_BUDGET_TOKENS_DEFAULT
	return max(0, resolved_budget_tokens)


def _assemble_prompt(retained_sections: list[PromptSection]) -> str:
	bodies = [body for _, _, body in retained_sections if body]
	return "\n\n".join(bodies)


def _estimate_prompt_tokens(prompt_text: str) -> int:
	if not prompt_text:
		return 0
	return (len(prompt_text) + CHARS_PER_TOKEN_ESTIMATE - 1) // CHARS_PER_TOKEN_ESTIMATE


def _retain_sections_within_budget(
	sections: list[PromptSection],
	budget_tokens: int,
) -> list[PromptSection]:
	copied_sections = [tuple(section) for section in sections]
	if _estimate_prompt_tokens(_assemble_prompt(copied_sections)) <= budget_tokens:
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
		retained_sections = [
			section
			for index, section in enumerate(copied_sections)
			if index not in dropped_indexes
		]
		if _estimate_prompt_tokens(_assemble_prompt(retained_sections)) <= budget_tokens:
			return retained_sections

	return [section for section in copied_sections if section[0] == 1]


def compact_if_over_budget(sections: list[PromptSection], budget_tokens: int | None) -> str:
	"""Return an assembled prompt from retained ``(tier, label, body)`` sections.

	Args:
		sections: Ordered ``(tier, label, body)`` tuples where tier ``1`` is
			keep-always, tier ``2`` is drop-after-tier-3, and tier ``3`` is
			drop-first when the assembled prompt exceeds the budget.
		budget_tokens: Explicit token budget, or ``None`` / any invalid value
			to fall back to ``OPENROUTER_PROMPT_BUDGET_TOKENS`` and then
			``160000``.

	Returns:
		A prompt string assembled from retained section bodies with blank lines
		between sections, preserving original order among retained sections.
	"""
	resolved_budget_tokens = _resolve_budget_tokens(budget_tokens)
	retained_sections = _retain_sections_within_budget(sections, resolved_budget_tokens)
	return _assemble_prompt(retained_sections)
