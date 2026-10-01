#!/usr/bin/env python3
"""Ledger for the escalation judge: which choices a failure may still get.

`/implement-plan-claude` stops at a CLAUDE.md §28.C failure escalation (a
cap reached, a security or validation run that did not succeed, a terminal
validation class, a security follow-up closed unmerged, a defective fix
check). Instead of waiting for a human, the stop hands its project checker
an `escalation` wait, and the checker starts `/escalation-judge` (CLAUDE.md
§28.G). The judge picks one choice from a fixed menu and never picks
`budget` or `descope` twice for the same failure (`close` stays available).
This script is that memory; the judge decides nothing without it.

The menu, in the order `allowed` returns it:

  budget   one more round of the capped loop, with a narrower fix
  descope  remove the failing part from the project (an `AD-<n>` entry)
  close    close the chain's PRs and the source issue as not planned

`close` is always available. A `budget` or `descope` already recorded for the
same stop id and fingerprint is never offered again, so once both are
used for a failure, only `close` remains. A different failure (a different
fingerprint) starts with the full menu.

The ledger is the `## Escalations` section of the project's progress log,
`docs/implement-plan/<slug>.md`, one line per decision:

  - ES-<n> [<stop id>, <YYYY-MM-DD>] fingerprint=<12 hex> choice=<budget|descope|close> why=<one line>

Subcommands:

  fingerprint --stop <id> (--evidence <json> | --evidence-file <path>)
      Normalise the failure evidence and print its fingerprint: the first
      12 hex characters of the SHA-1 of the canonical JSON of the stop id
      and the evidence. Evidence is a JSON object with any of these keys:
      `checks` (failing check names), `findings` (finding ids), `issues`
      (follow-up issue numbers), `validation_class`, `validation_status`,
      and `pr` (the number of the PR the stop is about, as `5164` or
      `#5164`). Strings are trimmed, lower-cased and whitespace-collapsed;
      lists are de-duplicated and sorted, so the order the evidence was read
      in never changes the fingerprint. Any other key is refused. `pr` is
      required for the PR-scoped stops (`intervention-cap`,
      `fix-check-defective`), so two PRs that fail the same way never share
      a fingerprint and never use up each other's choices.
  allowed --log <path> --stop <id> --fingerprint <fp>
      Parse `## Escalations` and print the choices not yet used for that
      stop and fingerprint, in the order budget, descope, close.
  record --log <path> --stop <id> --fingerprint <fp> --choice <c> (--why <text> | --why-file <path>) [--date YYYY-MM-DD]
      Print the `ES-<n>` line to append, with `n` one more than the highest
      id in the log. It never writes a file: the session adds the line to
      the log with its Edit tool. `--why-file` reads the rationale from a
      UTF-8 file, so text drawn from failure evidence never passes through
      shell quoting (a `$(...)` or backtick in a double-quoted `--why` would
      run before this script sees it). A `budget` or `descope` already used for
      that stop and fingerprint is refused, and so is a PR-scoped stop
      whose reason does not start with `PR #<N>: ` (the intervention cap
      counts an entry only for the PR its `why` names).

Output is one JSON line. Exit 0 on success; 1 on bad arguments (an unknown
stop id, a malformed fingerprint or evidence, a PR-scoped stop without
`pr` or without the `PR #<N>: ` prefix on its reason, not exactly one
readable `--why` / `--why-file`, a used choice); 2 when the
log cannot be read or an `## Escalations` line is malformed (a line that
starts `- ` but does not match the format, a date that is not a real
calendar date, or a repeated `ES-<n>` id). A log without an
`## Escalations` section has no entries.

No GitHub API calls and no network (CLAUDE.md §15): the script reads the
log file and its arguments only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

# The ten CLAUDE.md §28.C failure-escalation stops the judge answers, as
# `/implement-plan-claude` tags them (`<!-- ai:claude-blocked:v1 kind=escalation stop=<id> -->`).
STOP_IDS = (
	"intervention-cap",
	"conformance-cap",
	"fix-check-defective",
	"security-run-failed",
	"security-cap",
	"security-followup-unmerged",
	"validation-run-failed",
	"validation-cap",
	"validation-terminal",
	"verify-activation-cap",
)

# Human-only stops (operator decision Q8): never judged.
HUMAN_ONLY_KINDS = ("ask-first", "no-tools", "depth-limit")

CHOICES = ("budget", "descope", "close")

EVIDENCE_LIST_KEYS = ("checks", "findings", "issues")
EVIDENCE_SCALAR_KEYS = ("validation_class", "validation_status")
# The PR the stop is about. Without it, two PRs that fail the same way would
# share a fingerprint, and one PR's `budget` / `descope` would use up the
# other's choices.
EVIDENCE_PR_KEY = "pr"
# Stops that are about one PR: their fingerprint must carry `pr`.
PR_SCOPED_STOP_IDS = ("intervention-cap", "fix-check-defective")
PR_NUMBER_RE = re.compile(r"^#?([1-9][0-9]*)$")
# A PR-scoped entry's `why` names its PR first, so the intervention cap of
# one PR counts only the rounds granted for that PR.
WHY_PR_PREFIX_RE = re.compile(r"^PR #[1-9][0-9]*: \S")

FINGERPRINT_RE = re.compile(r"^[0-9a-f]{12}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ESCALATIONS_HEADING_RE = re.compile(r"^## Escalations\s*$")
NEXT_HEADING_RE = re.compile(r"^## ")
ENTRY_RE = re.compile(
	r"^- ES-(?P<n>[1-9][0-9]*) \[(?P<stop>[a-z-]+), (?P<date>\d{4}-\d{2}-\d{2})\] "
	r"fingerprint=(?P<fp>[0-9a-f]{12}) choice=(?P<choice>budget|descope|close) why=(?P<why>\S.*)$"
)


class UsageError(Exception):
	"""Bad arguments: exit 1."""


class LedgerError(Exception):
	"""An unreadable log or a malformed `## Escalations` line: exit 2."""


def _is_calendar_date(value: str) -> bool:
	"""True for a `YYYY-MM-DD` string that names a real calendar date."""
	if not DATE_RE.match(value):
		return False
	try:
		dt.date.fromisoformat(value)
	except ValueError:
		return False
	return True


class _Parser(argparse.ArgumentParser):
	def error(self, message: str) -> None:  # argparse exits 2 by default; 2 means a bad log here
		raise UsageError(message)


def _normalise_text(value: object) -> str:
	if isinstance(value, bool) or not isinstance(value, (str, int)):
		raise UsageError(f"evidence values must be strings or integers, got {value!r}")
	return " ".join(str(value).split()).lower()


def normalise_evidence(evidence: object) -> dict:
	"""Return the canonical form of the failure evidence."""
	if not isinstance(evidence, dict):
		raise UsageError("evidence must be a JSON object")
	known = EVIDENCE_LIST_KEYS + EVIDENCE_SCALAR_KEYS + (EVIDENCE_PR_KEY,)
	unknown = sorted(set(evidence) - set(known))
	if unknown:
		raise UsageError(f"unknown evidence keys: {unknown}; allowed: {list(known)}")
	canonical: dict = {}
	for key in EVIDENCE_LIST_KEYS:
		if key not in evidence:
			continue
		values = evidence[key]
		if not isinstance(values, list):
			raise UsageError(f"evidence key {key!r} must be a list")
		items = sorted({_normalise_text(item) for item in values} - {""})
		if items:
			canonical[key] = items
	for key in EVIDENCE_SCALAR_KEYS:
		if key not in evidence or evidence[key] is None:
			continue
		value = _normalise_text(evidence[key])
		if value:
			canonical[key] = value
	if evidence.get(EVIDENCE_PR_KEY) is not None:
		canonical[EVIDENCE_PR_KEY] = _normalise_pr(evidence[EVIDENCE_PR_KEY])
	return canonical


def _normalise_pr(value: object) -> str:
	"""The PR number as plain digits: `5164`, `"5164"` and `"#5164"` are the same PR."""
	match = PR_NUMBER_RE.match(_normalise_text(value))
	if not match:
		raise UsageError(f"evidence key 'pr' must be a PR number such as 5164 or '#5164', got {value!r}")
	return match.group(1)


def _check_stop(stop: str) -> str:
	if stop in HUMAN_ONLY_KINDS:
		raise UsageError(f"{stop!r} is a human-only stop (Q8): the escalation judge never acts on it")
	if stop not in STOP_IDS:
		raise UsageError(f"unknown stop id {stop!r}; expected one of {list(STOP_IDS)}")
	return stop


def _check_fingerprint(fingerprint: str) -> str:
	if not FINGERPRINT_RE.match(fingerprint):
		raise UsageError(f"fingerprint must be 12 lower-case hex characters, got {fingerprint!r}")
	return fingerprint


def fingerprint(stop: str, evidence: object) -> str:
	"""The failure fingerprint: 12 hex characters of SHA-1 over stop id and evidence."""
	payload = {"stop": _check_stop(stop), "evidence": normalise_evidence(evidence)}
	if stop in PR_SCOPED_STOP_IDS and EVIDENCE_PR_KEY not in payload["evidence"]:
		raise UsageError(f"stop {stop!r} is about one PR: the evidence must name it as 'pr'")
	canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
	return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def parse_entries(log_text: str) -> list[dict]:
	"""Parse the `## Escalations` section of a progress log."""
	entries: list[dict] = []
	in_section = False
	seen: set[int] = set()
	for number, line in enumerate(log_text.splitlines(), start=1):
		if ESCALATIONS_HEADING_RE.match(line):
			in_section = True
			continue
		if not in_section:
			continue
		if NEXT_HEADING_RE.match(line):
			break
		if not line.strip() or line[0] in " \t":
			continue
		match = ENTRY_RE.match(line)
		if not match or match.group("stop") not in STOP_IDS or not _is_calendar_date(match.group("date")):
			raise LedgerError(f"malformed ## Escalations line {number}: {line!r}")
		entry_id = int(match.group("n"))
		if entry_id in seen:
			raise LedgerError(f"repeated ES-{entry_id} on line {number}")
		seen.add(entry_id)
		entries.append(
			{
				"id": entry_id,
				"stop": match.group("stop"),
				"date": match.group("date"),
				"fingerprint": match.group("fp"),
				"choice": match.group("choice"),
				"why": match.group("why"),
			}
		)
	return entries


def _read_log(path: str) -> list[dict]:
	try:
		text = Path(path).read_text(encoding="utf-8")
	except (OSError, UnicodeDecodeError) as exc:
		raise LedgerError(f"cannot read --log {path}: {exc}") from exc
	return parse_entries(text)


def used_choices(entries: list[dict], stop: str, fp: str) -> list[str]:
	used = {entry["choice"] for entry in entries if entry["stop"] == stop and entry["fingerprint"] == fp}
	return [choice for choice in CHOICES if choice in used]


def allowed_choices(entries: list[dict], stop: str, fp: str) -> list[str]:
	"""Unused choices in menu order; `close` is always available."""
	used = set(used_choices(entries, stop, fp))
	return [choice for choice in CHOICES if choice == "close" or choice not in used]


def record_line(entries: list[dict], stop: str, fp: str, choice: str, why: str, date: str) -> dict:
	if choice not in CHOICES:
		raise UsageError(f"unknown choice {choice!r}; expected one of {list(CHOICES)}")
	if choice not in allowed_choices(entries, stop, fp):
		raise UsageError(f"choice {choice!r} is already recorded for stop {stop} fingerprint {fp}")
	why = " ".join(why.split())
	if not why:
		raise UsageError("--why must not be empty")
	if stop in PR_SCOPED_STOP_IDS and not WHY_PR_PREFIX_RE.match(why):
		raise UsageError(f"stop {stop!r} is about one PR: --why must start with 'PR #<N>: ', got {why!r}")
	if not _is_calendar_date(date):
		raise UsageError(f"--date must be a real calendar date as YYYY-MM-DD, got {date!r}")
	next_id = max((entry["id"] for entry in entries), default=0) + 1
	line = f"- ES-{next_id} [{stop}, {date}] fingerprint={fp} choice={choice} why={why}"
	return {"id": f"ES-{next_id}", "line": line}


def _load_evidence(args: argparse.Namespace) -> object:
	if (args.evidence is None) == (args.evidence_file is None):
		raise UsageError("pass exactly one of --evidence or --evidence-file")
	if args.evidence_file is not None:
		try:
			raw = Path(args.evidence_file).read_text(encoding="utf-8")
		except (OSError, UnicodeDecodeError) as exc:
			raise UsageError(f"cannot read --evidence-file: {exc}") from exc
	else:
		raw = args.evidence
	try:
		return json.loads(raw)
	except ValueError as exc:
		raise UsageError(f"evidence is not valid JSON: {exc}") from exc


def _load_why(args: argparse.Namespace) -> str:
	if (args.why is None) == (args.why_file is None):
		raise UsageError("pass exactly one of --why or --why-file")
	if args.why_file is None:
		return args.why
	try:
		return Path(args.why_file).read_text(encoding="utf-8")
	except (OSError, UnicodeDecodeError) as exc:
		raise UsageError(f"cannot read --why-file: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
	parser = _Parser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
	fp_cmd = sub.add_parser("fingerprint", help="print the failure fingerprint")
	fp_cmd.add_argument("--stop", required=True)
	fp_cmd.add_argument("--evidence")
	fp_cmd.add_argument("--evidence-file")
	allowed_cmd = sub.add_parser("allowed", help="print the choices not yet used for this failure")
	allowed_cmd.add_argument("--log", required=True)
	allowed_cmd.add_argument("--stop", required=True)
	allowed_cmd.add_argument("--fingerprint", required=True)
	record_cmd = sub.add_parser("record", help="print the ES-<n> line to append to the log")
	record_cmd.add_argument("--log", required=True)
	record_cmd.add_argument("--stop", required=True)
	record_cmd.add_argument("--fingerprint", required=True)
	record_cmd.add_argument("--choice", required=True)
	record_cmd.add_argument("--why")
	record_cmd.add_argument("--why-file")
	record_cmd.add_argument("--date")
	return parser


def run(argv: list[str] | None = None, today: dt.date | None = None) -> dict:
	args = build_parser().parse_args(argv)
	if args.command == "fingerprint":
		return {"stop": args.stop, "fingerprint": fingerprint(args.stop, _load_evidence(args))}
	stop = _check_stop(args.stop)
	fp = _check_fingerprint(args.fingerprint)
	entries = _read_log(args.log)
	if args.command == "allowed":
		return {
			"stop": stop,
			"fingerprint": fp,
			"allowed": allowed_choices(entries, stop, fp),
			"used": used_choices(entries, stop, fp),
		}
	date = args.date or (today or dt.datetime.now(dt.timezone.utc).date()).isoformat()
	return record_line(entries, stop, fp, args.choice, _load_why(args), date)


def main(argv: list[str] | None = None, today: dt.date | None = None) -> int:
	try:
		result = run(argv, today)
	except UsageError as exc:
		print(json.dumps({"error": str(exc)}))
		return 1
	except LedgerError as exc:
		print(json.dumps({"error": str(exc)}))
		return 2
	print(json.dumps(result))
	return 0


if __name__ == "__main__":
	sys.exit(main())
