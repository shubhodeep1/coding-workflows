#!/usr/bin/env python3
"""Advisory prompt-injection pattern roster for AI-memory candidate writes."""

from __future__ import annotations

import re
from typing import NamedTuple


class InjectionPattern(NamedTuple):
	name: str
	regex: re.Pattern[str]


INJECTION_PATTERNS: list[InjectionPattern] = [
	InjectionPattern(
		"ignore_previous_instructions",
		re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
	),
	InjectionPattern(
		"ignore_above_instructions",
		re.compile(r"ignore\s+(all\s+)?above\s+instructions", re.IGNORECASE),
	),
	InjectionPattern(
		"disregard_previous",
		re.compile(r"disregard\s+(all\s+)?previous", re.IGNORECASE),
	),
	InjectionPattern(
		"forget_instructions",
		re.compile(r"forget\s+(all\s+)?(your\s+)?instructions", re.IGNORECASE),
	),
	InjectionPattern(
		"override_system_or_previous_instructions",
		re.compile(r"override\s+(system|previous)\s+(prompt|instructions)", re.IGNORECASE),
	),
	InjectionPattern(
		"you_are_now",
		re.compile(r"you\s+are\s+now\s+(?:a|an|the)\s+", re.IGNORECASE),
	),
	InjectionPattern(
		"act_as_role",
		re.compile(r"act\s+as\s+(?:a|an|the)\s+(?!plan|phase|wave)", re.IGNORECASE),
	),
	InjectionPattern(
		"pretend_roleplay",
		re.compile(r"pretend\s+(?:you(?:'re| are)\s+|to\s+be\s+)", re.IGNORECASE),
	),
	InjectionPattern(
		"from_now_on_you_are",
		re.compile(r"from\s+now\s+on,?\s+you\s+(?:are|will|should|must)", re.IGNORECASE),
	),
	InjectionPattern(
		"reveal_system_prompt",
		re.compile(
			r"(?:print|output|reveal|show|display|repeat)\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions)",
			re.IGNORECASE,
		),
	),
	InjectionPattern(
		"chatml_tags",
		re.compile(r"</?(?:system|assistant|human)>", re.IGNORECASE),
	),
	InjectionPattern(
		"system_brackets",
		re.compile(r"\[SYSTEM\]", re.IGNORECASE),
	),
	InjectionPattern(
		"inst_brackets",
		re.compile(r"\[INST\]", re.IGNORECASE),
	),
	InjectionPattern(
		"sys_delimiters",
		re.compile(r"<<\s*SYS\s*>>", re.IGNORECASE),
	),
]


def scan(text: str) -> list[str]:
	content = str(text or "")
	if not content:
		return []
	return [pattern.name for pattern in INJECTION_PATTERNS if pattern.regex.search(content)]


__all__ = ["INJECTION_PATTERNS", "scan"]
