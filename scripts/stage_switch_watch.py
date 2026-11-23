#!/usr/bin/env python3
"""Report pipeline stage switches left off (issue #9994).

Many pipeline stages have an on/off switch read from a repository variable
(``vars.<NAME>_ENABLED || 'true'``). When an owner leaves one off, the stage
logs ``outcome=disabled`` and returns success, so nobody notices. This helper
is the logic half of ``scripts/stage_switch_watch.sh``, which runs daily in
``.github/workflows/workflow-log-analysis.yml`` (job ``collect-logs``).

Commands (stdlib only; values are never printed raw):

``snapshot --registry R``
    Reads JSON lines ``{"name", "value", "updated_at"}`` from stdin (the
    wrapper's one paginated ``actions/variables`` listing), keeps only the
    names listed in the registry and prints a sanitized snapshot JSON
    ``{name: {"value_class", "display_value", "updated_at"}}``. Values of
    variables not in the registry are dropped before anything is written.

``evaluate --registry R --snapshot S [--now ISO] [--wait-hours H]``
    Prints one ``STAGE_SWITCH_OFF`` line per off switch on stderr and a JSON
    result ``{"off": [...], "alerts": [...], "counts": {...}}`` on stdout.

Exit codes: 0 ok, 2 invalid registry, 3 unreadable listing or snapshot. The
wrapper turns every non-zero exit into one warning and exits 0 (fail-open).

Registry (``scripts/stage_switches.json``, schema ``stage_switches.v1``):
    ``switches``: ``[{"name", "default": "true", "stage"}]``
    ``off_on_purpose``: ``[{"name", "until": "YYYY-MM-DD", "reason"}]`` --
    a switch listed here does not alert before ``until`` (00:00 UTC); on and
    after that date it alerts again (``marker=expired``).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

REGISTRY_SCHEMA = "stage_switches.v1"
DEFAULT_WAIT_HOURS = 24.0
SWITCH_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Secret-like names are matched on whole `_`-separated words so that, for
# example, FINAL_CI_FAILED_REDISPATCH_ENABLED (which contains the letters
# "PAT" inside "DISPATCH") is not redacted. Assumption: the plan's substring
# list TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL|PAT is meant as words, since a
# substring match would hide the value of the very switch that motivated #9994.
SECRET_NAME_RE = re.compile(
	r"(?:^|_)(?:TOKENS?|SECRETS?|KEYS?|PASSWORDS?|CREDENTIALS?|PATS?)(?:_|$)"
)
BOOLEAN_DISPLAY_VALUES = frozenset({"true", "false", "0", "1", "yes", "no", "on", "off"})


class RegistryError(ValueError):
	"""The registry file is missing, unreadable or does not match the schema."""


def _warn(message: str) -> None:
	print(f"::warning::{message}", file=sys.stderr)


def _display_value(name: str, raw: object) -> str:
	"""Return a value that is safe to print: allowlisted booleans only."""
	if SECRET_NAME_RE.search(str(name or "").upper()):
		return "<redacted>"
	value = str(raw if raw is not None else "").strip().lower()
	if value in BOOLEAN_DISPLAY_VALUES:
		return value
	return "<non-boolean>"


def _value_class(raw: object) -> str:
	value = str(raw if raw is not None else "").strip().lower()
	return "on" if value == "true" else "off"


def _parse_date(text: object) -> _dt.datetime | None:
	if not isinstance(text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
		return None
	try:
		day = _dt.date.fromisoformat(text)
	except ValueError:
		return None
	return _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc)


def _parse_timestamp(text: object) -> _dt.datetime | None:
	if not isinstance(text, str) or not text.strip():
		return None
	value = text.strip()
	if value.endswith("Z"):
		value = value[:-1] + "+00:00"
	try:
		stamp = _dt.datetime.fromisoformat(value)
	except ValueError:
		return None
	if stamp.tzinfo is None:
		stamp = stamp.replace(tzinfo=_dt.timezone.utc)
	return stamp.astimezone(_dt.timezone.utc)


def load_registry(path: str | Path) -> dict:
	"""Load and validate the registry; raise RegistryError on any problem."""
	try:
		data = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError) as exc:
		raise RegistryError(f"cannot read registry {path}: {exc.__class__.__name__}") from None
	if not isinstance(data, dict) or data.get("schema") != REGISTRY_SCHEMA:
		raise RegistryError(f"registry schema is not {REGISTRY_SCHEMA}")
	switches = data.get("switches")
	if not isinstance(switches, list) or not switches:
		raise RegistryError("registry has no switches list")
	seen = set()
	for entry in switches:
		if not isinstance(entry, dict):
			raise RegistryError("registry switch entry is not an object")
		name = entry.get("name")
		if not isinstance(name, str) or not SWITCH_NAME_RE.fullmatch(name):
			raise RegistryError("registry switch entry has an invalid name")
		if name in seen:
			raise RegistryError(f"registry lists {name} twice")
		seen.add(name)
		if entry.get("default") != "true":
			raise RegistryError(f"registry switch {name} default is not 'true'")
		if not isinstance(entry.get("stage"), str) or not entry["stage"].strip():
			raise RegistryError(f"registry switch {name} has no stage text")
	markers = data.get("off_on_purpose", [])
	if not isinstance(markers, list):
		raise RegistryError("registry off_on_purpose is not a list")
	for marker in markers:
		if not isinstance(marker, dict):
			raise RegistryError("registry off_on_purpose entry is not an object")
	data.setdefault("off_on_purpose", [])
	return data


def build_snapshot(registry: dict, lines) -> dict:
	"""Keep only registry switches; never keep a raw value."""
	wanted = {entry["name"] for entry in registry["switches"]}
	snapshot = {}
	for line in lines:
		line = line.strip()
		if not line:
			continue
		record = json.loads(line)
		if not isinstance(record, dict):
			raise ValueError("variable record is not an object")
		name = record.get("name")
		if not isinstance(name, str) or name not in wanted:
			continue
		raw = record.get("value")
		updated_at = record.get("updated_at")
		snapshot[name] = {
			"value_class": _value_class(raw),
			"display_value": _display_value(name, raw),
			"updated_at": updated_at if isinstance(updated_at, str) else "",
		}
	return snapshot


def _valid_markers(registry: dict) -> dict:
	"""Map switch name -> (until datetime, reason); warn on and drop bad markers."""
	names = {entry["name"] for entry in registry["switches"]}
	markers = {}
	for marker in registry.get("off_on_purpose", []):
		name = marker.get("name")
		if not isinstance(name, str) or name not in names:
			_warn(f"STAGE_SWITCH_WATCH off_on_purpose marker for unknown switch {str(name)[:80]!r} ignored")
			continue
		until = _parse_date(marker.get("until"))
		if until is None:
			_warn(f"STAGE_SWITCH_WATCH off_on_purpose marker for {name} has an invalid until date; ignored")
			continue
		reason = marker.get("reason")
		markers[name] = (until, reason if isinstance(reason, str) else "")
	return markers


def evaluate(registry: dict, snapshot: dict, now: _dt.datetime, wait_hours: float) -> dict:
	markers = _valid_markers(registry)
	off = []
	alerts = []
	counts = {"off": 0, "waiting": 0, "on_purpose": 0, "alerts": 0}
	for entry in registry["switches"]:
		name = entry["name"]
		state = snapshot.get(name)
		if not isinstance(state, dict):
			continue  # unset: the default (true) applies
		if state.get("value_class") != "off":
			continue
		# Re-check the stored display value so a hand-edited snapshot cannot
		# smuggle a raw value into the log or the alert.
		display_value = state.get("display_value")
		if SECRET_NAME_RE.search(name):
			display_value = "<redacted>"
		elif display_value not in BOOLEAN_DISPLAY_VALUES and display_value != "<redacted>":
			display_value = "<non-boolean>"
		since = _parse_timestamp(state.get("updated_at"))
		hours_off = None if since is None else max(0.0, (now - since).total_seconds() / 3600.0)
		marker_state = "none"
		if name in markers:
			until, _reason = markers[name]
			marker_state = "valid" if now < until else "expired"
		if marker_state == "valid":
			status = "off_on_purpose"
			counts["on_purpose"] += 1
		elif hours_off is not None and hours_off < wait_hours:
			status = "waiting"
			counts["waiting"] += 1
		else:
			status = "alert"
		record = {
			"name": name,
			"display_value": display_value,
			"default": entry["default"],
			"stage": entry["stage"],
			"off_since": since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else "unknown",
			"hours_off": None if hours_off is None else round(hours_off, 1),
			"status": status,
			"marker": marker_state,
		}
		if marker_state != "none":
			record["until"] = markers[name][0].strftime("%Y-%m-%d")
		counts["off"] += 1
		off.append(record)
		if status == "alert":
			counts["alerts"] += 1
			alerts.append(record)
		hours_text = "unknown" if record["hours_off"] is None else f"{record['hours_off']}"
		print(
			f"STAGE_SWITCH_OFF name={name} value={display_value} default={entry['default']} "
			f"off_since={record['off_since']} hours_off={hours_text} status={status} marker={marker_state}",
			file=sys.stderr,
		)
	return {"off": off, "alerts": alerts, "counts": counts}


def _parse_wait_hours(text: str | None) -> float:
	if text is None or str(text).strip() == "":
		return DEFAULT_WAIT_HOURS
	value = str(text).strip()
	if not re.fullmatch(r"\d+(?:\.\d+)?", value):
		_warn(f"STAGE_SWITCH_WATCH wait hours {value[:20]!r} is not a non-negative number; using {DEFAULT_WAIT_HOURS:g}")
		return DEFAULT_WAIT_HOURS
	return float(value)


def _parse_now(text: str | None) -> _dt.datetime:
	if text:
		stamp = _parse_timestamp(text)
		if stamp is not None:
			return stamp
		_warn("STAGE_SWITCH_WATCH --now is not an ISO timestamp; using the current time")
	return _dt.datetime.now(_dt.timezone.utc)


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	snap = sub.add_parser("snapshot")
	snap.add_argument("--registry", required=True)
	ev = sub.add_parser("evaluate")
	ev.add_argument("--registry", required=True)
	ev.add_argument("--snapshot", required=True)
	ev.add_argument("--now", default="")
	ev.add_argument("--wait-hours", default=None)
	args = parser.parse_args(argv)

	try:
		registry = load_registry(args.registry)
	except RegistryError as exc:
		_warn(f"STAGE_SWITCH_WATCH registry invalid: {exc}")
		return 2

	if args.command == "snapshot":
		try:
			snapshot = build_snapshot(registry, sys.stdin)
		except (ValueError, UnicodeDecodeError) as exc:
			_warn(f"STAGE_SWITCH_WATCH variable listing could not be parsed ({exc.__class__.__name__})")
			return 3
		json.dump(snapshot, sys.stdout, sort_keys=True)
		sys.stdout.write("\n")
		return 0

	try:
		snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
	except (OSError, ValueError) as exc:
		_warn(f"STAGE_SWITCH_WATCH snapshot unreadable ({exc.__class__.__name__})")
		return 3
	if not isinstance(snapshot, dict):
		_warn("STAGE_SWITCH_WATCH snapshot is not an object")
		return 3
	result = evaluate(registry, snapshot, _parse_now(args.now), _parse_wait_hours(args.wait_hours))
	json.dump(result, sys.stdout, sort_keys=True)
	sys.stdout.write("\n")
	return 0


if __name__ == "__main__":
	sys.exit(main())
