"""Structured decision log: one `RUNNER_SCALER key=value ...` line per decision."""

from __future__ import annotations

import re
import sys
from typing import Any, Callable

PREFIX = "RUNNER_SCALER"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:/@+=,-]")

LogFn = Callable[..., None]


def _value(value: Any) -> str:
	text = str(value)
	text = _UNSAFE.sub("_", text)
	return text[:200] or "none"


def format_line(outcome: str, **fields: Any) -> str:
	parts = [PREFIX, f"outcome={_value(outcome)}"]
	parts.extend(f"{key}={_value(value)}" for key, value in fields.items() if value is not None)
	return " ".join(parts)


def log(outcome: str, **fields: Any) -> None:
	print(format_line(outcome, **fields), file=sys.stdout, flush=True)
