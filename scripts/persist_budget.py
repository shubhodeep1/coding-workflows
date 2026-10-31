#!/usr/bin/env python3
"""Budget and progress governor (issue #8427, phase P6 of
docs/plans/persistent-unattended-workflows-plan.md).

`decide` reads an issue's attempt journal (scripts/attempt_journal.py, P1) and
returns one decision for the poller:

	continue   within budget; normal stall recovery goes on
	replan     re-trigger plan generation with a new approach
	escalate   add ai:needs-human and comment (budget exhausted)
	ignore     nothing to decide (security item, needs-human, closed, no
		journal, or this journal state was already decided)

Checks, in order:
	0. any label ai:security-pass-failed or containing "security", or
		ai:needs-human / ai:closed                       -> ignore
	1. no trusted journal entries                         -> ignore journal_absent
	2. newest entry is a `governor` escalation (outcome blocked)
		-> ignore already_decided
	3. oldest entry (after the newest governor escalation, if any) older
		than PERSIST_MAX_HOURS                            -> escalate max_hours
		(a newest `governor` re-plan entry otherwise -> ignore already_decided)
	4. rounds on the current approach >= PERSIST_MAX_ROUNDS, unless the issue
		is labelled ai:ready-to-merge (max_rounds), or
		the newest PERSIST_REPLAN_AFTER_FAILED_ROUNDS rounds all failed
		(failed_rounds), or the newest 2 rounds both failed with no progress
		between them (no_progress)                       -> replan, or escalate
		max_approaches:<trigger> once PERSIST_MAX_APPROACHES approaches exist;
		a would-be re-plan of an issue labelled ai:implementing, ai:done,
		ai:review-blocked or ai:ready-to-merge (open PR; plan.yml skips
		planning) escalates
		open_pr:<trigger> instead
	5. newest round is a failed `postmerge` round with no newer `plan` entry
		(P5 hand-off)                                     -> replan / escalate
		postmerge_recurred
	6. otherwise                                          -> continue within_budget

A round is a journal entry of stage implement, review, resolver or postmerge
on the current approach. It failed when its outcome is `fail` or its
acceptance state is `unmet`. Progress between two rounds is any of: a
different failure_key, fewer unmet acceptance items, fewer PR-caused gate
failures (both known), or a different evidence_hash.

Switches (each defaulted here, unattended instructions section 8):
	PERSIST_BUDGET_ENABLED              default false. Off: `decide` logs
			`PERSIST governor outcome=skipped reason=disabled` and
			writes nothing.
	PERSIST_MAX_APPROACHES              default 3, clamped to 1..20
	PERSIST_MAX_ROUNDS                  default 5, clamped to 1..50
	PERSIST_MAX_HOURS                   default 48, clamped to 1..720
	PERSIST_REPLAN_AFTER_FAILED_ROUNDS  default 3, clamped to 1..20

CLI (always exits 0; makes no API call):
	decide (--journal-json F | --comments-file F --trusted-login L) --labels JSON
		[--now TS] [--caps-json JSON] --out F

The decision JSON `{action, reason, basis, approaches, rounds, failed_streak,
summary}` is written to --out; nothing is written on error. `summary` is built
only from host-computed fields (counts, stage/outcome tokens, 12-hex failure
keys, caps) and never from journal `note` text, because the poller posts it in
a `/answer` comment that plan.yml reads as answers.

Log lines (stderr, tokens only):
	PERSIST governor outcome=<skipped|error> reason=<token>
The poller logs the decision itself (`outcome=<action> reason=<r> issue=<n>`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
	import attempt_journal  # noqa: E402
except Exception:  # noqa: BLE001 - fail open below
	attempt_journal = None  # type: ignore[assignment]

SECURITY_LABEL = "ai:security-pass-failed"
IGNORE_LABELS = ("ai:needs-human", "ai:closed")
# Phases with an open implementation PR: plan.yml skips planning for them.
# ai:implementing is included because implement.yml swaps it for ai:done only
# best-effort after opening the PR; an implement run still in progress is
# skipped earlier by the poller's active-workflow check.
OPEN_PR_LABELS = ("ai:implementing", "ai:done", "ai:review-blocked", "ai:ready-to-merge")
# The PR passed review and is waiting to merge: the rounds cap never pulls it back.
MERGING_LABEL = "ai:ready-to-merge"
ROUND_STAGES = ("implement", "review", "resolver", "postmerge")
NO_PROGRESS_ROUNDS = 2
ACTIONS = ("continue", "replan", "escalate", "ignore")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HEX12_RE = re.compile(r"^[0-9a-f]{12}$")
TOKEN_RE = re.compile(r"[^A-Za-z0-9_.:-]")
# name: (default, low, high)
CAP_SPECS = {
	"max_approaches": ("PERSIST_MAX_APPROACHES", 3, 1, 20),
	"max_rounds": ("PERSIST_MAX_ROUNDS", 5, 1, 50),
	"max_hours": ("PERSIST_MAX_HOURS", 48, 1, 720),
	"replan_after": ("PERSIST_REPLAN_AFTER_FAILED_ROUNDS", 3, 1, 20),
}


def log(outcome: str, reason: str, issue: Any = None) -> None:
	suffix = f" issue={issue}" if issue not in (None, "") else ""
	print(f"PERSIST governor outcome={outcome} reason={reason}{suffix}", file=sys.stderr)


def _flag(name: str) -> bool:
	return os.environ.get(name, "false").strip().lower() == "true"


def _clamp(value: Any, default: int, low: int, high: int) -> int:
	if isinstance(value, bool):
		value = default
	elif isinstance(value, str):
		text = value.strip()
		try:
			value = int(text) if text else default
		except ValueError:
			value = default
	elif not isinstance(value, int):
		value = default
	return max(low, min(high, value))


def _int_env(name: str, default: int, low: int, high: int) -> int:
	return _clamp(os.environ.get(name, ""), default, low, high)


def enabled() -> bool:
	return _flag("PERSIST_BUDGET_ENABLED")


def caps(overrides: Any = None) -> dict:
	"""Caps from the environment (defaults here), then optional overrides,
	every value clamped to its range."""
	out = {}
	for key, (env, default, low, high) in CAP_SPECS.items():
		out[key] = _int_env(env, default, low, high)
		if isinstance(overrides, dict) and key in overrides:
			out[key] = _clamp(overrides[key], out[key], low, high)
	return out


def parse_ts(value: Any) -> datetime | None:
	if not isinstance(value, str) or not TS_RE.match(value):
		return None
	try:
		return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
	except ValueError:
		return None


def _labels(raw: Any) -> list[str]:
	out = []
	for item in raw if isinstance(raw, list) else []:
		if isinstance(item, dict):
			item = item.get("name")
		if isinstance(item, str) and item:
			out.append(item)
	return out


def ignored(labels: Any) -> str:
	"""The ignore reason token for these labels, or "" when none applies."""
	names = _labels(labels)
	for name in names:
		if name == SECURITY_LABEL or "security" in name.lower():
			return "security_label"
	if "ai:needs-human" in names:
		return "needs_human"
	if "ai:closed" in names:
		return "closed"
	return ""


def open_pr_label(labels: Any) -> bool:
	return any(name in OPEN_PR_LABELS for name in _labels(labels))


def _ordered(entries: Any) -> list[dict]:
	items = [e for e in (entries if isinstance(entries, list) else []) if isinstance(e, dict) and parse_ts(e.get("ts"))]
	return sorted(items, key=lambda e: e["ts"])


def _approach(entry: dict) -> int:
	value = entry.get("approach")
	return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def current_approach(entries: list[dict]) -> int:
	ordered = _ordered(entries)
	plans = [e for e in ordered if e.get("stage") == "plan"]
	if plans:
		return _approach(plans[-1])
	for entry in reversed(ordered):
		if _approach(entry):
			return _approach(entry)
	return 0


def approaches(entries: list[dict]) -> int:
	return len({_approach(e) for e in _ordered(entries) if _approach(e)})


def rounds(entries: list[dict], approach: int) -> list[dict]:
	return [e for e in _ordered(entries) if e.get("stage") in ROUND_STAGES and _approach(e) == approach]


def _acceptance(entry: dict) -> dict:
	value = entry.get("acceptance")
	return value if isinstance(value, dict) else {}


def _gate(entry: dict) -> dict:
	value = entry.get("gate")
	return value if isinstance(value, dict) else {}


def failed(entry: dict) -> bool:
	return entry.get("outcome") == "fail" or _acceptance(entry).get("state") == "unmet"


def _unmet_count(entry: dict) -> int:
	unmet = _acceptance(entry).get("unmet")
	return len(unmet) if isinstance(unmet, list) else 0


def _pr_caused(entry: dict) -> int | None:
	value = _gate(entry).get("pr_caused")
	return value if isinstance(value, int) and not isinstance(value, bool) else None


def progressed(prev: dict, cur: dict) -> bool:
	if (prev.get("failure_key") or "") != (cur.get("failure_key") or ""):
		return True
	if _unmet_count(cur) < _unmet_count(prev):
		return True
	before, after = _pr_caused(prev), _pr_caused(cur)
	if before is not None and after is not None and after < before:
		return True
	return (prev.get("evidence_hash") or "") != (cur.get("evidence_hash") or "")


def trailing_failed(items: list[dict]) -> int:
	count = 0
	for entry in reversed(items):
		if not failed(entry):
			break
		count += 1
	return count


def basis_key(entries: list[dict]) -> str:
	base = [e for e in _ordered(entries) if e.get("stage") != "governor"]
	if not base:
		return ""
	if attempt_journal is not None:
		return attempt_journal.latest_key(base)
	newest = base[-1]
	return "|".join((str(newest.get("stage", "")), str(newest.get("run_id", "")), str(newest.get("attempt", ""))))


def _tok(value: Any) -> str:
	return TOKEN_RE.sub("_", str(value))[:40]


def summary(entries: list[dict], decision: dict, limits: dict) -> str:
	"""One paragraph of host-computed fields only (never `note` text)."""
	approach = current_approach(entries)
	items = rounds(entries, approach)
	recent = ", ".join(f"{_tok(e.get('stage'))}={_tok(e.get('outcome'))}" for e in items[-3:]) or "none"
	keys = []
	for entry in _ordered(entries):
		key = entry.get("failure_key")
		if isinstance(key, str) and HEX12_RE.match(key) and key not in keys:
			keys.append(key)
	key_text = ", ".join(f"`{k}`" for k in keys[-5:]) or "none"
	return (
		f"Decision: {_tok(decision.get('action'))} ({_tok(decision.get('reason'))}). "
		f"Approaches tried: {approaches(entries)} of {limits['max_approaches']}. "
		f"Rounds on the current approach: {len(items)} of {limits['max_rounds']} "
		f"(newest: {recent}; failed in a row: {trailing_failed(items)}, re-plan after {limits['replan_after']}). "
		f"Distinct failure keys: {key_text}. Time limit: {limits['max_hours']} hours."
	)


def _result(entries: list[dict], action: str, reason: str, limits: dict) -> dict:
	approach = current_approach(entries)
	items = rounds(entries, approach)
	decision = {
		"action": action,
		"reason": reason,
		"basis": basis_key(entries),
		"approaches": approaches(entries),
		"rounds": len(items),
		"failed_streak": trailing_failed(items),
	}
	decision["summary"] = summary(entries, decision, limits)
	return decision


def decide(entries: Any, now: datetime, labels: Any, limits: dict | None = None) -> dict:
	limits = limits or caps()
	ordered = _ordered(entries)
	reason = ignored(labels)
	if reason:
		return _result(ordered, "ignore", reason, limits)
	if not ordered:
		return _result(ordered, "ignore", "journal_absent", limits)
	newest_governor = ordered[-1].get("stage") == "governor"
	# A newest governor escalation (outcome `blocked`) is final. A newest
	# governor re-plan whose plan run never happened (plan.yml skipped it)
	# still escalates once the time cap is reached, as the plan orders
	# max_hours first.
	if newest_governor and ordered[-1].get("outcome") == "blocked":
		return _result(ordered, "ignore", "already_decided", limits)
	# The time window starts at the first entry after the newest governor
	# escalation, so work a person resumed gets a fresh window instead of
	# being escalated again on its first idle tick.
	start = 0
	for index, entry in enumerate(ordered):
		if entry.get("stage") == "governor" and entry.get("outcome") == "blocked":
			start = index + 1
	oldest = parse_ts(ordered[start]["ts"])
	if oldest is not None and (now - oldest).total_seconds() > limits["max_hours"] * 3600:
		return _result(ordered, "escalate", "max_hours", limits)
	if newest_governor:
		return _result(ordered, "ignore", "already_decided", limits)
	can_replan = approaches(ordered) < limits["max_approaches"]
	items = rounds(ordered, current_approach(ordered))
	trigger = ""
	# max_rounds counts every round, whatever its outcome (review rounds are
	# journalled `ok` because each one is an autofix commit); only a PR that
	# passed review and waits to merge is never pulled back.
	if len(items) >= limits["max_rounds"] and MERGING_LABEL not in _labels(labels):
		trigger = "max_rounds"
	elif trailing_failed(items) >= limits["replan_after"]:
		trigger = "failed_rounds"
	elif len(items) >= NO_PROGRESS_ROUNDS and all(failed(e) for e in items[-NO_PROGRESS_ROUNDS:]) and not any(
		progressed(items[i - 1], items[i]) for i in range(len(items) - NO_PROGRESS_ROUNDS + 1, len(items))
	):
		trigger = "no_progress"
	if not trigger:
		all_rounds = [e for e in ordered if e.get("stage") in ROUND_STAGES]
		if all_rounds and all_rounds[-1].get("stage") == "postmerge" and all_rounds[-1].get("outcome") == "fail":
			newest_round_ts = all_rounds[-1]["ts"]
			if not any(e.get("stage") == "plan" and e["ts"] > newest_round_ts for e in ordered):
				trigger = "postmerge_recurred"
	if trigger:
		if can_replan and open_pr_label(labels):
			# plan.yml skips planning while the issue's PR is open, so a
			# re-plan here could never run; hand it to a person instead.
			return _result(ordered, "escalate", f"open_pr:{trigger}", limits)
		if can_replan:
			return _result(ordered, "replan", trigger, limits)
		return _result(ordered, "escalate", f"max_approaches:{trigger}", limits)
	return _result(ordered, "continue", "within_budget", limits)


def _load_entries(args: argparse.Namespace) -> list[dict] | None:
	if args.journal_json:
		try:
			data = json.loads(Path(args.journal_json).read_text(encoding="utf-8"))
		except (OSError, ValueError):
			return None
		if isinstance(data, dict):
			data = data.get("entries")
		if not isinstance(data, list):
			return None
		if attempt_journal is not None:
			return [e for e in (attempt_journal.normalise_entry(raw) for raw in data) if e]
		return [e for e in data if isinstance(e, dict)]
	if not args.comments_file or attempt_journal is None:
		return None
	comments = attempt_journal.load_comments(args.comments_file, None, None)
	if comments is None:
		return None
	_, entries = attempt_journal.parse(comments, (args.trusted_login or "").strip())
	return entries


def cmd_decide(args: argparse.Namespace) -> None:
	try:
		labels = json.loads(args.labels or "[]")
	except ValueError:
		log("error", "labels_invalid")
		return
	overrides = None
	if args.caps_json:
		try:
			overrides = json.loads(args.caps_json)
		except ValueError:
			log("error", "caps_invalid")
			return
	if args.now:
		now = parse_ts(args.now)
		if now is None:
			log("error", "now_invalid")
			return
	else:
		now = datetime.now(timezone.utc)
	entries = _load_entries(args)
	if entries is None:
		log("error", "journal_unreadable")
		return
	decision = decide(entries, now, labels, caps(overrides))
	try:
		Path(args.out).write_text(json.dumps(decision, sort_keys=True), encoding="utf-8")
	except OSError:
		log("error", "out_unwritable")


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Persistence budget governor (P6).")
	sub = parser.add_subparsers(dest="cmd", required=True)
	dec = sub.add_parser("decide")
	dec.add_argument("--journal-json")
	dec.add_argument("--comments-file")
	dec.add_argument("--trusted-login", default="")
	dec.add_argument("--labels", default="[]")
	dec.add_argument("--now", default="")
	dec.add_argument("--caps-json", default="")
	dec.add_argument("--out", required=True)
	return parser


def main(argv: list[str] | None = None) -> int:
	try:
		args = build_parser().parse_args(argv)
	except SystemExit:
		log("error", "usage")
		return 0
	if not enabled():
		log("skipped", "disabled")
		return 0
	try:
		cmd_decide(args)
	except Exception as exc:  # noqa: BLE001 - fail open, never block the tick
		log("error", "exception_" + type(exc).__name__)
	return 0


if __name__ == "__main__":
	sys.exit(main())
