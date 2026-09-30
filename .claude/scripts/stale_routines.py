#!/usr/bin/env python3
"""Deterministic stale-Routine sweep for the check-in flows (CLAUDE.md §26.G).

The CLAUDE.md §26 status check-in and the `/implement-plan-claude` Check-in
Loop create Routines (claude-code-remote triggers): the checker's `send_later`
reminders, the `/implement-plan-claude` safety net, and the hand-back Routine
bound to the session that pushed the PR. Fired one-shots, Routines whose
session is gone, and hand-backs whose PR finished long ago pile up unless
something deletes them. This script decides which ones to delete; the model
only lists the Routines, runs this script, and calls `delete_trigger` on each
id it prints. The script never deletes anything itself.

Input: `--triggers FILE`, a JSON file holding the `list_triggers` result
(`{"data": [...]}`) or its bare `data` array, listed with
`include_completed: true`. When the result is too large for the context,
the harness saves it to a file; pass that file as is. Only these fields of
each Routine are read: `id`, `name`, `enabled`, `ended_reason`, and the
prompt (`derived_state.prompt`, or a top-level `prompt`). Anything else may
be left out.

Output: one JSON object on stdout, e.g.

  {"delete": [{"id": "trig_…", "name": "PR #12 status check-in",
               "reason": "ended: run_once_fired"}],
   "kept": 3, "not_ours": 5, "errors": []}

Exit status: 0 when a verdict was reached (errors on individual PR reads are
listed in `errors` and those Routines are kept), 2 when the input file is
missing or not the expected JSON shape.

Only Routines these flows create are ever eligible, matched by name:

  * `PR #<n> status check-in…`     (§26 checker reminders)
  * `PR #<n> hand-back`            (§26 hand-back)
  * `implement-plan <slug>: …`     (/implement-plan-claude, hand-back included)
  * `dispatch <owner>/<repo>#<n>: …` (the Claude dispatcher's one-shot start
                                    of an issue or PR fixer session, §26.H)

Routine names are capped at 60 characters and truncated with `…`, so a name
never carries the repository. A hand-back is recognised by its prompt, which
starts with `… hand-back for …` and names the PR URL
(`https://github.com/<owner>/<repo>/pull/<n>`); the PR is read from there.

Every other Routine counts as `not_ours` and is never named for deletion.
An eligible Routine is named when either:

  1. it has ended (`ended_reason` set: `run_once_fired`,
     `auto_disabled_session_gone`, …). A Routine the user paused is disabled
     with no `ended_reason` and is kept; or
  2. it is an enabled hand-back whose pull request merged or closed more than
     `--grace-hours` (default 24) ago. The grace leaves the checker, which
     reads the PR every 3 hours, time to hand the verdict back first.

API budget (CLAUDE.md §15): REST only, never GraphQL, one `gh api
repos/<owner>/<repo>/pulls/<n>` call per distinct enabled hand-back PR, and
no call at all for the other rules. A failed read keeps the Routine and is
reported in `errors` (fail safe: never delete on missing data).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys

DEFAULT_GRACE_HOURS = 24.0

CHECK_IN_NAME_PATTERN = re.compile(r"^PR #\d+ (?:status check-in|hand-back)")
IMPLEMENT_PLAN_NAME_PATTERN = re.compile(r"^implement-plan \S+: ")
DISPATCH_NAME_PATTERN = re.compile(r"^dispatch [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+: ")
HAND_BACK_PROMPT_PATTERN = re.compile(
	r"hand-back for .*?https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<pr>\d+)",
	re.IGNORECASE | re.DOTALL,
)


class RoutineReadError(Exception):
	"""A `gh api` read failed; the Routine it concerned is kept."""


def gh_api(path: str) -> dict:
	"""GET one REST path through `gh api` and return the decoded JSON object."""
	try:
		proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise RoutineReadError(f"gh api {path} failed: {exc}") from exc
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout).strip().splitlines()
		raise RoutineReadError(f"gh api {path} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	try:
		payload = json.loads(proc.stdout)
	except ValueError as exc:
		raise RoutineReadError(f"gh api {path} returned invalid JSON") from exc
	if not isinstance(payload, dict):
		raise RoutineReadError(f"gh api {path} returned non-object JSON")
	return payload


def is_ours(name: str) -> bool:
	"""Return True when `name` is a Routine one of the check-in flows creates."""
	return bool(CHECK_IN_NAME_PATTERN.search(name) or IMPLEMENT_PLAN_NAME_PATTERN.search(name)
		or DISPATCH_NAME_PATTERN.search(name))


def routine_prompt(routine: dict) -> str:
	"""Return the Routine's prompt from `derived_state.prompt` or a top-level `prompt`."""
	derived_state = routine.get("derived_state")
	if isinstance(derived_state, dict) and isinstance(derived_state.get("prompt"), str):
		return derived_state["prompt"]
	prompt = routine.get("prompt")
	return prompt if isinstance(prompt, str) else ""


def _parse_time(value: object) -> dt.datetime:
	if not isinstance(value, str):
		raise ValueError(f"timestamp must be a string, got {type(value).__name__}")
	return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load_routines(path: str) -> list[dict]:
	with open(path, encoding="utf-8") as handle:
		payload = json.load(handle)
	routines = payload.get("data") if isinstance(payload, dict) else payload
	if not isinstance(routines, list) or any(not isinstance(item, dict) for item in routines):
		raise ValueError("expected the list_triggers result or its `data` array of objects")
	return routines


def _terminal_pr_age_hours(repo: str, number: int, now: dt.datetime, pr_cache: dict) -> float | None:
	"""Hours since the PR merged or closed, or None while it is open (one cached read per PR)."""
	key = (repo, number)
	if key not in pr_cache:
		pr_cache[key] = gh_api(f"repos/{repo}/pulls/{number}")
	pr = pr_cache[key]
	if pr.get("merged"):
		ended_at = pr.get("merged_at")
	elif pr.get("state") == "closed":
		ended_at = pr.get("closed_at")
	else:
		return None
	return (now - _parse_time(ended_at)).total_seconds() / 3600


def classify(routines: list[dict], grace_hours: float, now: dt.datetime) -> dict:
	"""Split `routines` into the ones to delete and counts of the rest."""
	to_delete: list[dict] = []
	errors: list[str] = []
	kept = 0
	not_ours = 0
	pr_cache: dict = {}
	for routine in routines:
		routine_id = routine.get("id")
		name = routine.get("name")
		if not isinstance(routine_id, str) or not isinstance(name, str) or not is_ours(name):
			not_ours += 1
			continue
		ended_reason = routine.get("ended_reason") or ""
		if ended_reason:
			to_delete.append({"id": routine_id, "name": name, "reason": f"ended: {ended_reason}"})
			continue
		hand_back = HAND_BACK_PROMPT_PATTERN.search(routine_prompt(routine))
		if hand_back and routine.get("enabled") is True:
			repo = hand_back.group("repo")
			number = int(hand_back.group("pr"))
			try:
				age_hours = _terminal_pr_age_hours(repo, number, now, pr_cache)
			except (RoutineReadError, KeyError, TypeError, ValueError) as exc:
				errors.append(f"{routine_id} ({name}): {exc}")
				kept += 1
				continue
			if age_hours is not None and age_hours >= grace_hours:
				to_delete.append(
					{
						"id": routine_id,
						"name": name,
						"reason": f"{repo}#{number} finished {age_hours:.1f}h ago (>= {grace_hours:g}h)",
					}
				)
				continue
		kept += 1
	return {"delete": to_delete, "kept": kept, "not_ours": not_ours, "errors": errors}


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--triggers", required=True, help="JSON file with the list_triggers result (include_completed: true)")
	parser.add_argument("--grace-hours", type=float, default=DEFAULT_GRACE_HOURS)
	return parser


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		routines = _load_routines(args.triggers)
	except (OSError, ValueError) as exc:
		print(json.dumps({"delete": [], "error": f"cannot read --triggers: {exc}"}))
		return 2
	now = now or dt.datetime.now(dt.timezone.utc)
	print(json.dumps(classify(routines, args.grace_hours, now)))
	return 0


if __name__ == "__main__":
	sys.exit(main())
