#!/usr/bin/env python3
"""Report pipeline stage switches left off (issue #9994).

Many pipeline stages have an on/off switch read from a repository variable
(``vars.<NAME>_ENABLED || 'true'``). When an owner leaves one off, the stage
logs ``outcome=disabled`` and returns success, so nobody notices. This helper
is the logic half of ``scripts/stage_switch_watch.sh``, which runs daily in
``.github/workflows/workflow-log-analysis.yml`` (job ``collect-logs``).

Commands (stdlib only; values are never printed raw):

``snapshot --registry R [--repo-root DIR]``
    Reads JSON lines ``{"name", "value", "updated_at"}`` from stdin (the
    wrapper's one paginated ``actions/variables`` listing), keeps only the
    names of the (merged) switch list and prints a sanitized snapshot JSON
    ``{name: {"value_class", "display_value", "updated_at"}}``. Values of
    variables not in the switch list are dropped before anything is written.

``evaluate --registry R --snapshot S [--repo-root DIR] [--now ISO] [--wait-hours H]``
    Prints one ``STAGE_SWITCH_OFF`` line per off switch on stderr and a JSON
    result ``{"off": [...], "alerts": [...], "counts": {...}}`` on stdout.

``--repo-root DIR`` (issue #10127) turns on discovery: every
``vars.<NAME>_ENABLED || 'true'`` expression (also the chained
``|| (...) || 'true'`` and multi-line forms) and every
``vars.<NAME>_ENABLED != 'false'`` test in ``DIR/.github/workflows/*.yml``
and ``*.yaml`` (comment lines ignored), and every ``_ENABLED`` row with
default ``true`` in the ``DIR/README.md`` tables ``#### Variables`` and
``## Required Variables`` (minus ``README_NOT_REPOSITORY_VARIABLES``), is
watched without a registry entry. Discovery reads files only and fails open:
a missing or unreadable file adds nothing. Without ``--repo-root`` only the
registry entries are watched (the behaviour before #10127).

Exit codes: 0 ok, 2 invalid registry, 3 unreadable listing or snapshot. The
wrapper turns every non-zero exit into one warning and exits 0 (fail-open).

Registry (``scripts/stage_switches.json``, schema ``stage_switches.v1``):
    ``switches``: ``[{"name"[, "default": "true"][, "stage"][, "on_values"][, "pending"]}]``
    Only ``name`` is required (#10127). An entry's ``stage`` and
    ``on_values`` win over the generated detail of a discovered switch; a
    discovered switch without an entry, or an entry without ``stage``, gets
    the generated text "Turns on <name in words> (<files>); false turns that
    stage off.". ``pending: true`` (a boolean) marks an entry or marker for a
    switch that is not on the default branch yet; the registry test accepts
    an undiscovered name only with it.
    ``on_values`` (optional): extra lowercase values, from ``1``, ``yes`` and
    ``on``, that the switch's consumers also treat as on. Without it only
    ``true`` counts as on. Set it only when every consumer of the switch
    keeps the stage on for those values (for example ``!= 'false'``).
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
# Values a registry entry may add to "on" through its optional on_values list.
ALLOWED_EXTRA_ON_VALUES = frozenset({"1", "yes", "on"})

# Discovery (issue #10127), moved from tests/test_stage_switch_registry.py.
# Matched against each workflow's whole text (comment lines removed), with
# `\s` between tokens, so an expression split across lines is still found.
SWITCH_DEFAULT_RE = re.compile(r"vars\.([A-Z][A-Z0-9_]*_ENABLED)\s*\|\|\s*(?:\([^()]*\)\s*\|\|\s*)*'([^']*)'")
# `vars.<NAME>_ENABLED != 'false'` keeps a stage on unless the variable is
# `false`, so it is a default-on switch too.
SWITCH_NOT_FALSE_RE = re.compile(r"vars\.([A-Z][A-Z0-9_]*_ENABLED)\s*!=\s*'false'")
README_SWITCH_TABLES = ("#### Variables", "## Required Variables")
# README rows with default `true` that no workflow reads from a repository
# variable (the watch reads repository variables only). Each needs a reason.
README_NOT_REPOSITORY_VARIABLES = {
	"CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED": "scripts/resolve_integration_ref.sh environment only; no workflow forwards a repository variable",
	"LESSONS_LEARNED_ENABLED": "job environment only (README Memory System); no workflow reads vars.LESSONS_LEARNED_ENABLED",
	"WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED": "scripts/workflow_failure_heal_intake.sh environment only; not forwarded from a repository variable",
}


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


def _value_class(raw: object, on_values=()) -> str:
	value = str(raw if raw is not None else "").strip().lower()
	return "on" if value == "true" or value in on_values else "off"


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


def _scan_workflow_switches(workflows_dir: str | Path) -> dict[str, dict[str, set[str]]]:
	"""Map each default-on workflow switch to {"files": set, "forms": set}.

	Fails open: a missing folder gives {} and an unreadable file is skipped.
	"""
	found: dict[str, dict[str, set[str]]] = {}
	folder = Path(workflows_dir)
	if not folder.is_dir():
		return found
	# `.yaml` workflows are scanned too, so a switch cannot hide in one.
	for workflow in sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")]):
		try:
			raw = workflow.read_text(encoding="utf-8")
		except (OSError, UnicodeDecodeError):
			continue
		text = "\n".join(
			line for line in raw.splitlines()
			if not line.strip().startswith("#")
		)
		for name, default in SWITCH_DEFAULT_RE.findall(text):
			if default == "true":
				item = found.setdefault(name, {"files": set(), "forms": set()})
				item["files"].add(workflow.name)
				item["forms"].add("default_true")
		for name in SWITCH_NOT_FALSE_RE.findall(text):
			item = found.setdefault(name, {"files": set(), "forms": set()})
			item["files"].add(workflow.name)
			item["forms"].add("not_false")
	return found


def _workflow_true_switches(workflows_dir: str | Path) -> dict[str, set[str]]:
	"""Map each switch defaulting to 'true' in some workflow to those files."""
	return {name: set(item["files"]) for name, item in _scan_workflow_switches(workflows_dir).items()}


def _readme_variables_rows(readme_path: str | Path, heading: str = "#### Variables") -> dict[str, list[str]] | None:
	"""Map each `| `NAME` |` row of the table under heading to its Default cells.

	Returns None when the README, the heading (exactly once) or the
	`| Variable ` header row with a Default column is missing.
	"""
	try:
		lines = Path(readme_path).read_text(encoding="utf-8").splitlines()
	except (OSError, UnicodeDecodeError):
		return None
	starts = [i for i, line in enumerate(lines) if line == heading]
	if len(starts) != 1:
		return None
	rows: dict[str, list[str]] = {}
	# The tables differ ("| Variable | Required | Default |" vs
	# "| Variable | Default |"), so take the Default column from the header.
	default_col = None
	for line in lines[starts[0] + 1:]:
		if re.match(r"^#", line):
			break
		cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
		if line.startswith("| Variable "):
			if "Default" not in cells:
				return None
			default_col = cells.index("Default")
			continue
		if not line.startswith("| `") or default_col is None or len(cells) <= default_col:
			continue
		rows.setdefault(cells[0].strip("`"), []).append(cells[default_col].replace("`", "").strip())
	if default_col is None:
		return None
	return rows


def _readme_true_switches(readme_path: str | Path) -> set[str]:
	"""`_ENABLED` rows with a Default starting with `true` (before exclusions)."""
	found: set[str] = set()
	for heading in README_SWITCH_TABLES:
		rows = _readme_variables_rows(readme_path, heading)
		if not rows:
			continue
		for name, defaults in rows.items():
			if name.endswith("_ENABLED") and any(d.lower().startswith("true") for d in defaults):
				found.add(name)
	return found


def discover_switches(repo_root: str | Path) -> dict[str, dict]:
	"""Discover default-on switches in a checkout (issue #10127).

	Returns {name: {"files": [...], "forms": [...]}}: files are the sorted
	workflow file names, or ["README.md"] when only the README lists the
	switch; forms is a sorted subset of default_true, not_false and readme.
	"""
	root = Path(repo_root)
	merged: dict[str, dict[str, set[str]]] = {}
	for name, item in _scan_workflow_switches(root / ".github" / "workflows").items():
		merged[name] = {"files": set(item["files"]), "forms": set(item["forms"])}
	for name in _readme_true_switches(root / "README.md"):
		if name in README_NOT_REPOSITORY_VARIABLES:
			continue
		item = merged.setdefault(name, {"files": set(), "forms": set()})
		item["forms"].add("readme")
	result = {}
	for name in sorted(merged):
		if not SWITCH_NAME_RE.fullmatch(name) or not name.endswith("_ENABLED"):
			continue
		item = merged[name]
		files = sorted(item["files"]) or ["README.md"]
		result[name] = {"files": files, "forms": sorted(item["forms"])}
	return result


def _generated_stage_text(name: str, files) -> str:
	"""Stage text of the existing generated form for a switch without one."""
	base = name[: -len("_ENABLED")] if name.endswith("_ENABLED") else name
	words = base.lower().replace("_", " ").strip()
	file_list = ", ".join(files or [])
	if file_list:
		return f"Turns on {words} ({file_list}); false turns that stage off."
	return f"Turns on {words}; false turns that stage off."


def load_registry(path: str | Path, repo_root: str | Path | None = None) -> dict:
	"""Load and validate the registry; raise RegistryError on any problem.

	With repo_root, the discovered switches are merged in (issue #10127):
	an entry's stage and on_values win, a discovered name without an entry
	gets generated text, and the merged list is sorted by name.
	"""
	try:
		data = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError) as exc:
		raise RegistryError(f"cannot read registry {path}: {exc.__class__.__name__}") from None
	if not isinstance(data, dict) or data.get("schema") != REGISTRY_SCHEMA:
		raise RegistryError(f"registry schema is not {REGISTRY_SCHEMA}")
	switches = data.get("switches")
	if not isinstance(switches, list):
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
		# default and stage are optional since #10127; when present they keep
		# today's rules.
		if "default" in entry and entry["default"] != "true":
			raise RegistryError(f"registry switch {name} default is not 'true'")
		if "stage" in entry and (not isinstance(entry["stage"], str) or not entry["stage"].strip()):
			raise RegistryError(f"registry switch {name} has no stage text")
		on_values = entry.get("on_values", [])
		if not isinstance(on_values, list) or not all(
			isinstance(value, str) and value in ALLOWED_EXTRA_ON_VALUES for value in on_values
		):
			raise RegistryError(f"registry switch {name} on_values must list only 1, yes or on")
		if "pending" in entry and not isinstance(entry["pending"], bool):
			raise RegistryError(f"registry switch {name} pending must be true or false")
	markers = data.get("off_on_purpose", [])
	if not isinstance(markers, list):
		raise RegistryError("registry off_on_purpose is not a list")
	for marker in markers:
		if not isinstance(marker, dict):
			raise RegistryError("registry off_on_purpose entry is not an object")
		if "pending" in marker and not isinstance(marker["pending"], bool):
			raise RegistryError("registry off_on_purpose pending must be true or false")
	data.setdefault("off_on_purpose", [])

	discovered = discover_switches(repo_root) if repo_root else {}
	merged = []
	for entry in switches:
		item = dict(entry)
		item["default"] = "true"
		if "stage" not in item:
			files = discovered.get(item["name"], {}).get("files", [])
			item["stage"] = _generated_stage_text(item["name"], files)
		merged.append(item)
	for name, info in discovered.items():
		if name in seen:
			continue
		merged.append({"name": name, "default": "true", "stage": _generated_stage_text(name, info["files"])})
	if repo_root:
		merged.sort(key=lambda item: item["name"])
	if not merged:
		raise RegistryError("registry has no switches list")
	data["switches"] = merged
	return data


def build_snapshot(registry: dict, lines) -> dict:
	"""Keep only registry switches; never keep a raw value."""
	wanted = {entry["name"]: frozenset(entry.get("on_values", [])) for entry in registry["switches"]}
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
			"value_class": _value_class(raw, wanted[name]),
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
			# A `pending: true` marker names a switch not on the default branch
			# yet (#10127); drop it without the daily warning. A missing or
			# malformed name can never be such a switch, so it still warns.
			if marker.get("pending") is True and isinstance(name, str) and SWITCH_NAME_RE.fullmatch(name):
				continue
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
	snap.add_argument("--repo-root", default="")
	ev = sub.add_parser("evaluate")
	ev.add_argument("--registry", required=True)
	ev.add_argument("--snapshot", required=True)
	ev.add_argument("--repo-root", default="")
	ev.add_argument("--now", default="")
	ev.add_argument("--wait-hours", default=None)
	args = parser.parse_args(argv)

	try:
		registry = load_registry(args.registry, repo_root=args.repo_root or None)
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
