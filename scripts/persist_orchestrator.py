#!/usr/bin/env python3
"""Orchestrator context for persistent unattended work (issue #8427, P7).

Switch: PERSIST_ORCH_CONTEXT_ENABLED (default "false", defaulted here as well
as in orchestrate.yml / orchestrate_poll.yml; unattended instructions section 8).

Subcommands (every one exits 0 except on an argparse usage error):

since-note --state-file F --current-wave N [--details-file D] [--trusted-login L] --out O
	Pure: no subprocess, no network, zero API calls. Renders the
	``## Since this plan was decomposed`` section for a wave-N issue body from
	the orchestrator state only:
	- merged items of waves 1..N-1 (issue, ``persist_merged_pr``, safe
		``files_touched`` paths, at most 10 per item);
	- judge-added items of waves 1..N-1 (entries the poller marked
		``persist_judge_added: true`` when the wave judge added them), any status;
	- the newest attempt-journal step/outcome (host fields only, never
		``note``) of an item whose comments are in the per-tick details cache
		``D`` (``comments_available: true``), parsed by ``attempt_journal.parse``
		for the pipeline login ``L`` only.
	Issue / PR numbers are plain (``issue 123``), never ``#123``: a ``#N``
	creates cross-reference timeline events the poller reads to find linked
	PRs, and risks auto-close phrasing. Every data line goes through
	``attempt_journal._scrub``. At most 4096 UTF-8 bytes, cut at item
	boundaries with a ``- ... K more items not shown`` trailer. Switch off,
	N <= 1 or nothing to report: nothing is written (O stays absent or empty).

open-projects --repo R --out O
	Exactly one REST read through ``gh_api_retry.call(optional=True)`` (honours
	the rate-limit breaker): open ``ai:orchestrator-tracking`` issues, first page
	of 50. Pull requests and authors whose ``author_association`` is not
	OWNER / MEMBER / COLLABORATOR are dropped; at most 20 titles (titles only,
	no issue numbers), each without
	control characters, cut to 120 characters and scrubbed, inside the
	``=== BEGIN/END UNTRUSTED OTHER OPEN PROJECTS ===`` fence. Any failure
	writes ``(open projects unavailable: <reason>)`` inside the fence.
	Switch off: no call, no output.

plan-path --description-file F [--repo-root .]
	Prints the first ``*.md`` path named in F that is a regular file inside the
	repo root reached without any symlink; prints nothing otherwise.

group --state-file F --journals-json D --now EPOCH --wave-index I [--trusted-login L] --out O
	Pure: no subprocess, no network, zero API calls. Switch:
	PERSIST_ORCH_FAILURE_GROUPING_ENABLED (default "false"; only the exact
	value "true" turns it on). Windows: PERSIST_ORCH_GROUP_WINDOW_HOURS
	(default 24) and PERSIST_ORCH_GROUP_MAX_HOURS (default 12); a value that is
	not a positive integer falls back to the default. Groups the open items of
	``state.waves[I].issues[]`` (integer ``github_issue``, status not
	``merged`` / ``closed`` / ``skipped`` / ``not_created``) whose newest
	trusted attempt-journal entry (``attempt_journal.parse`` for login ``L``
	over the per-tick details cache ``D``) is a ``fail`` / ``partial`` /
	``blocked`` outcome with a 12-hex ``failure_key`` no older than the window.
	Two or more items with the same key form a group; the owner is the item
	with the earliest failing entry of that key in the window (ties: lower
	issue), and an existing ``grouped_under.owner`` that is still in the group
	is kept. A ``grouped_under`` field is released when the item is no longer
	open, its key changed or aged out, its owner merged / closed / left or
	changed key, or ``now - since >= MAX_HOURS``. An item (or its owner)
	whose details-cache entry is missing or unreadable is "unknown": its field
	is kept unchanged, and only the MAX_HOURS bound applies. A ``max_hours`` release
	reports the key under ``release_cooldown`` and the item is not grouped
	again on that key (``persist_group_released_key``) until its key changes
	(``clear_cooldown``). Writes, only with the switch on:
	``{"groups": [{"key", "owner", "members"}], "release": [issue],
	"release_reasons": {"<issue>": token}, "release_cooldown": [{"issue",
	"key"}], "clear_cooldown": [issue], "new": [issue],
	"newest_failure_ts": {"<issue>": epoch}}`` (``members`` omits the owner;
	``new`` lists members without ``grouped_under`` before;
	``newest_failure_ts`` gives, for each new member, the later start of its
	own and its owner's newest unbroken run of same-key failing entries, so the
	poller can tell a pause comment of this episode from an older one). A stored ``grouped_under.key`` that is not 12 hex characters is
	released as ``field_invalid``. Model
	``note`` text is never read. Logs one ``PERSIST orch_group
	outcome=<ok|skipped|error> reason=<token> [groups=<n>] [grouped=<n>]
	[released=<n>]`` line (issue #8427, P8).

wave-check --check-runs-file F --now EPOCH [--wait-since ISO8601Z] [--required-names CSV] --out O
	Pure: no subprocess, no network, zero API calls. Switch:
	PERSIST_ORCH_WAVE_CHECK_ENABLED (default "false"; only the exact value
	"true" turns it on). PERSIST_ORCH_WAVE_CHECK_MAX_WAIT_MINUTES (default 90;
	a value that is not a positive integer falls back to 90). Classifies one
	page of ``GET repos/<r>/commits/<sha>/check-runs?per_page=100`` (an object
	with ``check_runs[]`` or an array of such pages): as in
	scripts/pr_checks_lib.sh, a newer run of the same ``name`` supersedes only a
	``cancelled`` run, then ``skipped`` / ``neutral`` conclusions are dropped.
	``--required-names`` uses the final-merge gate's sentinels (``*``, the
	default: every failure blocks; empty: nothing blocks; else a comma list):
	a completed non-``success`` run of a required name is ``red``, any run not
	completed is ``pending``, and ``red`` wins. ``green`` needs at least one
	run left and a complete listing (a truncated page is ``pending``);
	``no_checks`` when none is left; ``unavailable`` for unreadable input. With a valid past ``--wait-since`` and ``waited_minutes >= max``,
	``pending`` becomes ``timeout`` and ``no_checks`` becomes
	``no_checks_timeout``. Writes ``{"schema": "wave_check.v1", "outcome",
	"failing": [{"name", "conclusion"}], "pending": [name], "log_job_id",
	"summary_text", "waited_minutes", "max_wait_minutes", "wait_since_valid",
	"truncated"}`` (``failing`` / ``pending`` at most 20 each; ``log_job_id``
	is the id of the first failing ``github-actions`` run; ``summary_text`` the
	raw ``output.summary`` of the first failing other run). Issue #8427, P9.

wave-check-block --result-file R [--log-file L] --state-file S --wave-index I [--journals-json D] [--trusted-login L] --out O
	Pure: no subprocess, no network, zero API calls. Same switch. Only for a
	``red`` / ``timeout`` result: renders the
	``=== BEGIN/END UNTRUSTED WAVE CHECK ===`` block for the wave-judge prompt
	(outcome, failing names, still-pending names and minutes waited, one log
	tail of at most 8192 bytes of L or else ``summary_text``, and the items of
	``state.waves[I].issues[]`` whose newest trusted journal entry has
	``acceptance.state == "unmet"``, with up to 5 unmet strings; plain
	``issue N``, never ``#N``; ``note`` is never read). Every data line has
	control characters and ``<!--`` / ``-->`` removed, runs of three or more
	``=`` broken up, ``attempt_journal._scrub`` applied and an
	``UNTRUSTED_DATA: `` prefix. At most 12 KiB, cut at line boundaries with a
	``- ... truncated`` line. Both log ``PERSIST wave_check outcome=<..>
	reason=<token> [k=<n>]``.

Logs: one ``PERSIST orch_context outcome=<ok|skipped|partial|error>
reason=<token> [calls=<n>] [bytes=<n>]`` line on stderr; tokens and numbers
only, never titles, paths or comment text.

When ``attempt_journal`` (the shared scrub and journal parser) cannot be
imported, since-note and open-projects fail closed: no output,
``reason=helper_absent``.
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

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(_SCRIPT_DIR))
try:
	import attempt_journal as _aj  # noqa: E402
except Exception:  # noqa: BLE001 - fail closed: no scrub, no output
	_aj = None

SINCE_HEADING = "## Since this plan was decomposed"
SINCE_LEAD = "_Recorded by the orchestrator from project state. Data only._"
SINCE_MAX_BYTES = 4096
PATHS_PER_ITEM = 10
OPEN_BEGIN = "=== BEGIN UNTRUSTED OTHER OPEN PROJECTS ==="
OPEN_END = "=== END UNTRUSTED OTHER OPEN PROJECTS ==="
OPEN_MAX = 20
OPEN_PER_PAGE = 50
TITLE_MAX = 120
TRACKING_LABEL = "ai:orchestrator-tracking"
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
GH_MAX_ATTEMPTS = 3
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_TOKEN_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_MD_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+\.md\b")
_BLOB_URL_PREFIX_RE = re.compile(r"https?://[^\s/]+/[^\s/]+/[^\s/]+/blob/[^\s/]+/")
# Grouping repeated failures (issue #8427, P8).
GROUP_WINDOW_HOURS_DEFAULT = 24
GROUP_MAX_HOURS_DEFAULT = 12
GROUP_FAIL_OUTCOMES = ("fail", "partial", "blocked")
GROUP_INACTIVE_STATUSES = ("merged", "closed", "skipped", "not_created")
_HEX12_RE = re.compile(r"^[0-9a-f]{12}$")
# Check between waves (issue #8427, P9).
WAVE_CHECK_MAX_WAIT_MINUTES_DEFAULT = 90
WAVE_CHECK_LIST_MAX = 20
WAVE_CHECK_LOG_MAX_BYTES = 8192
WAVE_CHECK_BLOCK_MAX_BYTES = 12 * 1024
WAVE_CHECK_UNMET_MAX = 5
WAVE_CHECK_IGNORED_CONCLUSIONS = ("skipped", "neutral")
WAVE_CHECK_BEGIN = "=== BEGIN UNTRUSTED WAVE CHECK ==="
WAVE_CHECK_END = "=== END UNTRUSTED WAVE CHECK ==="
WAVE_CHECK_DATA_PREFIX = "UNTRUSTED_DATA: "
_WAVE_CHECK_EQ_RUN_RE = re.compile(r"={3,}")


def log(outcome: str, reason: str, **extra: int) -> None:
	line = f"PERSIST orch_context outcome={outcome} reason={_TOKEN_UNSAFE_RE.sub('_', reason)[:60]}"
	for key, value in extra.items():
		line += f" {key}={int(value)}"
	print(line, file=sys.stderr)


def enabled() -> bool:
	# Exactly "true", the same rule as the orchestrate.yml gates and the poller,
	# so a value like "1", "yes" or "TRUE " is off everywhere.
	return os.environ.get("PERSIST_ORCH_CONTEXT_ENABLED", "false") == "true"


def group_enabled() -> bool:
	# Exactly "true", the same rule as enabled() and the poller gate.
	return os.environ.get("PERSIST_ORCH_FAILURE_GROUPING_ENABLED", "false") == "true"


def _env_hours(name: str, default: int) -> int:
	value = _positive_int(os.environ.get(name, str(default)))
	return value if value is not None else default


def log_group(outcome: str, reason: str, **extra: int) -> None:
	line = f"PERSIST orch_group outcome={outcome} reason={_TOKEN_UNSAFE_RE.sub('_', reason)[:60]}"
	for key, value in extra.items():
		line += f" {key}={int(value)}"
	print(line, file=sys.stderr)


def _positive_int(value: Any) -> int | None:
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value if value > 0 else None
	if isinstance(value, str) and value.isdigit() and value[0] != "0":
		return int(value)
	return None


def safe_path(value: Any) -> str | None:
	if not isinstance(value, str) or not _SAFE_PATH_RE.match(value):
		return None
	if value.startswith("/") or ".." in value.split("/"):
		return None
	return value


def _local_id(value: Any) -> str:
	text = _ID_UNSAFE_RE.sub("_", str(value if value is not None else ""))[:40]
	return text or "item"


def _write(path: str, text: str) -> bool:
	try:
		Path(path).write_text(text, encoding="utf-8")
		return True
	except OSError:
		return False


def _read_json(path: str | None) -> Any:
	if not path:
		return None
	try:
		return json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None


def _newest_journal_step(entry_gh: int | None, details: Any, login: str) -> tuple[str, str] | None:
	if entry_gh is None or not login or not isinstance(details, dict):
		return None
	issue = details.get(str(entry_gh))
	if not isinstance(issue, dict) or issue.get("comments_available") is not True:
		return None
	comments = issue.get("comments")
	if not isinstance(comments, list):
		return None
	_cid, entries = _aj.parse(comments, login)
	if not entries:
		return None
	newest = sorted(entries, key=lambda e: e.get("ts", ""))[-1]
	stage = newest.get("stage")
	outcome = newest.get("outcome")
	if stage not in _aj.STAGES or outcome not in _aj.OUTCOMES:
		return None
	return stage, outcome


def since_lines(state: Any, current_wave: int, details: Any = None, login: str = "") -> tuple[list[str], int]:
	"""Item lines for waves 1..current_wave-1 and how many carry a journal step."""
	lines: list[str] = []
	journal_hits = 0
	if not isinstance(state, dict) or not isinstance(state.get("waves"), list):
		return lines, journal_hits
	for wave in state["waves"][: max(0, current_wave - 1)]:
		issues = wave.get("issues") if isinstance(wave, dict) else None
		if not isinstance(issues, list):
			continue
		for entry in issues:
			if not isinstance(entry, dict):
				continue
			status = entry.get("status")
			# Explicit marker written at the wave-judge additions in
			# orchestrate_poll_process.sh; state entries never carry a title.
			judge_added = entry.get("persist_judge_added") is True
			if status != "merged" and not judge_added:
				continue
			gh = _positive_int(entry.get("github_issue"))
			line = f"- `{_local_id(entry.get('id'))}`: " + (f"issue {gh}" if gh else "issue not created")
			if judge_added:
				line += " (added by the wave judge)"
			if status == "merged":
				pr = _positive_int(entry.get("persist_merged_pr"))
				line += f", merged as PR {pr}" if pr else ", merged, PR unknown"
			else:
				line += f", status `{_TOKEN_UNSAFE_RE.sub('_', str(status if status is not None else 'unknown'))[:30]}`"
			raw_files = entry.get("files_touched") if isinstance(entry.get("files_touched"), list) else []
			files = [p for p in (safe_path(f) for f in raw_files) if p]
			if files:
				shown = files[:PATHS_PER_ITEM]
				line += ". Files: " + ", ".join(f"`{p}`" for p in shown)
				if len(files) > len(shown):
					line += f" +{len(files) - len(shown)} more"
			step = _newest_journal_step(gh, details, login)
			if step:
				journal_hits += 1
				line += f", latest journal step `{step[0]}` -> `{step[1]}`"
			lines.append(_aj._scrub(line))
	return lines, journal_hits


def render_since(lines: list[str], max_bytes: int = SINCE_MAX_BYTES) -> str:
	if not lines:
		return ""
	head = SINCE_HEADING + "\n\n" + SINCE_LEAD + "\n\n"
	body = "\n".join(lines) + "\n"
	if len((head + body).encode("utf-8")) <= max_bytes:
		return head + body
	kept: list[str] = []
	for index, line in enumerate(lines):
		trailer = f"- ... {len(lines) - index - 1} more items not shown\n"
		candidate = head + "".join(k + "\n" for k in kept + [line]) + trailer
		if len(candidate.encode("utf-8")) > max_bytes:
			break
		kept.append(line)
	hidden = len(lines) - len(kept)
	return head + "".join(k + "\n" for k in kept) + f"- ... {hidden} more items not shown\n"


def cmd_since_note(args: argparse.Namespace) -> None:
	if not enabled():
		log("skipped", "switch_off")
		return
	if _aj is None:
		log("skipped", "helper_absent")
		return
	try:
		current_wave = int(str(args.current_wave).strip())
	except ValueError:
		log("error", "wave_invalid")
		return
	if current_wave <= 1:
		log("skipped", "first_wave")
		return
	state = _read_json(args.state_file)
	if not isinstance(state, dict):
		log("error", "state_unreadable")
		return
	details = _read_json(args.details_file)
	login = args.trusted_login or ""
	lines, _journal_hits = since_lines(state, current_wave, details, login)
	text = render_since(lines)
	if not text:
		_write(args.out, "")
		log("skipped", "nothing_to_report", calls=0)
		return
	if not _write(args.out, text):
		log("error", "out_unwritable")
		return
	# journal_absent: no trusted login or no details cache, so no journal lines.
	reason = "rendered" if login and isinstance(details, dict) else "journal_absent"
	log("ok", reason, calls=0, bytes=len(text.encode("utf-8")))


def _clean_title(value: str) -> str:
	text = " ".join(_CONTROL_RE.sub(" ", value).split())[:TITLE_MAX]
	return _aj._scrub(text)


def open_project_lines(payload: Any) -> list[str]:
	lines: list[str] = []
	if not isinstance(payload, list):
		return lines
	for item in payload:
		if len(lines) >= OPEN_MAX:
			break
		if not isinstance(item, dict) or "pull_request" in item:
			continue
		if item.get("author_association") not in TRUSTED_ASSOCIATIONS:
			continue
		number = _positive_int(item.get("number"))
		title = item.get("title")
		if number is None or not isinstance(title, str):
			continue
		lines.append(f"- {_clean_title(title)}")
	return lines


def _fence(lines: list[str]) -> str:
	return OPEN_BEGIN + "\n" + "".join(line + "\n" for line in lines) + OPEN_END + "\n"


def cmd_open_projects(args: argparse.Namespace) -> None:
	if not enabled():
		log("skipped", "switch_off", calls=0)
		return
	if _aj is None:
		log("skipped", "helper_absent", calls=0)
		return
	repo = args.repo or ""
	if not _REPO_RE.match(repo):
		_write(args.out, _fence(["(open projects unavailable: repo_invalid)"]))
		log("partial", "repo_invalid", calls=0)
		return
	try:
		import gh_api_retry  # noqa: PLC0415
	except Exception:  # noqa: BLE001 - no raw fallback: it would bypass the breaker
		_write(args.out, _fence(["(open projects unavailable: gh_api_retry_unavailable)"]))
		log("partial", "gh_api_retry_unavailable", calls=0)
		return
	endpoint = f"repos/{repo}/issues?labels={TRACKING_LABEL}&state=open&per_page={OPEN_PER_PAGE}"
	try:
		result = gh_api_retry.call([endpoint], idempotent=True, optional=True, max_attempts=GH_MAX_ATTEMPTS)
		raw = result.stdout if result.rc == 0 else None
		reason = "" if result.rc == 0 else "api_" + _TOKEN_UNSAFE_RE.sub("_", str(result.kind))[:30]
	except Exception as exc:  # noqa: BLE001 - fail open, never block the decomposer
		raw, reason = None, "exception_" + type(exc).__name__
	payload: Any = None
	if raw is not None:
		try:
			payload = json.loads(raw.decode("utf-8", "replace"))
		except ValueError:
			payload = None
		if not isinstance(payload, list):
			reason = "response_invalid"
	if reason:
		_write(args.out, _fence([f"(open projects unavailable: {reason})"]))
		log("partial", reason, calls=1)
		return
	lines = open_project_lines(payload) or ["(no other open projects)"]
	text = _fence(lines)
	if not _write(args.out, text):
		log("error", "out_unwritable", calls=1)
		return
	log("ok", "rendered", calls=1, bytes=len(text.encode("utf-8")))


def _ts_epoch(value: Any) -> int | None:
	if not isinstance(value, str):
		return None
	try:
		return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
	except ValueError:
		return None


def _newest_failure(entries: list[dict], now: int, window_s: int) -> tuple[str, int, int] | None:
	"""(failure_key, earliest failing ts of that key in the window, start ts of
	the newest unbroken run of failing entries with that key) when the newest
	entry is a failure inside the window, else None. The run start does not move
	when the same failure repeats, so it marks when the episode began; a gap
	longer than the window between two entries of the run ends it."""
	if not entries:
		return None
	ordered = sorted(entries, key=lambda e: e.get("ts", ""))
	newest = ordered[-1]
	key = newest.get("failure_key")
	if newest.get("outcome") not in GROUP_FAIL_OUTCOMES or not isinstance(key, str) or not _HEX12_RE.match(key):
		return None
	newest_ts = _ts_epoch(newest.get("ts"))
	if newest_ts is None or newest_ts < now - window_s:
		return None
	stamps = [
		t for t in (_ts_epoch(e.get("ts")) for e in ordered if e.get("outcome") in GROUP_FAIL_OUTCOMES and e.get("failure_key") == key)
		if t is not None and t >= now - window_s
	]
	run_start = newest_ts
	prev_t = newest_ts
	for e in reversed(ordered):
		if e.get("outcome") not in GROUP_FAIL_OUTCOMES or e.get("failure_key") != key:
			break
		t = _ts_epoch(e.get("ts"))
		if t is None:
			continue
		# A gap longer than the window ends the run: an older same-key failure
		# that had already aged out belongs to an earlier episode.
		if prev_t - t > window_s:
			break
		run_start = min(run_start, t)
		prev_t = t
	return key, (min(stamps) if stamps else newest_ts), run_start


def group_failures(
	state: Any, wave_index: int, details: Any, login: str, now: int, window_hours: int, max_hours: int,
) -> tuple[dict, bool]:
	"""Grouping decision for state.waves[wave_index] and whether any trusted
	journal entry was seen."""
	result: dict = {"groups": [], "release": [], "release_reasons": {}, "release_cooldown": [], "clear_cooldown": [], "new": []}
	issues: list = []
	waves = state.get("waves") if isinstance(state, dict) else None
	if isinstance(waves, list) and 0 <= wave_index < len(waves) and isinstance(waves[wave_index], dict):
		raw = waves[wave_index].get("issues")
		issues = raw if isinstance(raw, list) else []
	window_s = window_hours * 3600
	max_s = max_hours * 3600
	items: dict[int, dict] = {}
	journal_seen = False
	for entry in issues:
		if not isinstance(entry, dict):
			continue
		gh = _positive_int(entry.get("github_issue"))
		if gh is None or gh in items:
			continue
		active = entry.get("status") not in GROUP_INACTIVE_STATUSES
		entries: list[dict] = []
		cached = details.get(str(gh)) if isinstance(details, dict) else None
		# A missing or unreadable cache entry (a failed GraphQL batch, or the
		# whole cache reset to {}) means "unknown", not "the failure went away".
		readable = bool(login) and isinstance(cached, dict) and cached.get("comments_available") is True and isinstance(cached.get("comments"), list)
		if active and readable:
			_cid, entries = _aj.parse(cached["comments"], login)
		journal_seen = journal_seen or bool(entries)
		failure = _newest_failure(entries, now, window_s) if active else None
		items[gh] = {
			"entry": entry,
			"active": active,
			"key": failure[0] if failure else None,
			"earliest": failure[1] if failure else 0,
			"newest_ts": failure[2] if failure else 0,
			"has_journal": bool(entries),
			"unknown": active and not readable,
		}

	def old_group(gh: int) -> dict | None:
		value = items[gh]["entry"].get("grouped_under")
		return value if isinstance(value, dict) else None

	release: dict[int, str] = {}
	cooldown: dict[int, str] = {}
	clear: list[int] = []
	blocked: set[int] = set()
	held: set[int] = set()
	for gh, it in items.items():
		if "persist_group_released_key" not in it["entry"]:
			continue
		if it["key"] is not None and it["key"] == it["entry"].get("persist_group_released_key"):
			blocked.add(gh)
		elif not it["active"] or it["has_journal"]:
			clear.append(gh)
	for gh, it in items.items():
		if "grouped_under" not in it["entry"]:
			continue
		old = old_group(gh)
		owner = _positive_int(old.get("owner")) if old else None
		owner_it = items.get(owner) if owner is not None else None
		since = old.get("since") if old else None
		if old is None or owner is None or not isinstance(old.get("key"), str) or not _HEX12_RE.match(old["key"]):
			reason = "field_invalid"
		elif not it["active"]:
			reason = "item_inactive"
		elif owner_it is None:
			reason = "owner_gone"
		elif not owner_it["active"] or _positive_int(owner_it["entry"].get("persist_merged_pr")) is not None:
			reason = "owner_merged"
		elif it["unknown"] or (owner_it["unknown"] and it["key"] == old.get("key")):
			# Journal state unknown this tick: keep the field as it is (only the
			# max_hours bound still applies), so a transient cache gap neither
			# un-pauses the item nor resets since / re-posts the comment later.
			if not isinstance(since, bool) and isinstance(since, int) and now - since >= max_s:
				reason = "max_hours"
				cooldown[gh] = old["key"]
			else:
				held.add(gh)
				continue
		elif not journal_seen:
			reason = "journal_absent"
		elif it["key"] is None or it["key"] != old.get("key"):
			reason = "key_changed"
		elif owner_it["key"] != old.get("key"):
			reason = "owner_key_changed"
		elif isinstance(since, bool) or not isinstance(since, int):
			reason = "field_invalid"
		elif now - since >= max_s:
			reason = "max_hours"
			cooldown[gh] = it["key"]
		else:
			continue
		release[gh] = reason

	buckets: dict[str, list[int]] = {}
	for gh in sorted(items):
		it = items[gh]
		if it["key"] is None or gh in blocked or gh in release or gh in held:
			continue
		buckets.setdefault(it["key"], []).append(gh)
	grouped: set[int] = set()
	episode_ts: dict[int, int] = {}
	for key in sorted(buckets):
		members = buckets[key]
		if len(members) < 2:
			continue
		# Held members (cache entry unknown this tick) still vouch for the
		# owner they recorded, so a one-tick cache gap does not re-elect it.
		prior = sorted({
			o for o in (_positive_int((old_group(g) or {}).get("owner")) for g in members + sorted(held) if (old_group(g) or {}).get("key") == key)
			if o in members
		})
		owner = prior[0] if prior else min(members, key=lambda g: (items[g]["earliest"], g))
		if "grouped_under" in items[owner]["entry"]:
			release[owner] = "owner_changed"
		kept: list[int] = []
		for gh in members:
			if gh == owner:
				continue
			old = old_group(gh)
			if "grouped_under" in items[gh]["entry"] and (old is None or _positive_int(old.get("owner")) != owner):
				release[gh] = "owner_changed"
				continue
			kept.append(gh)
		if not kept:
			continue
		grouped.update(kept)
		result["groups"].append({"key": key, "owner": owner, "members": kept})
		for gh in kept:
			if "grouped_under" not in items[gh]["entry"]:
				result["new"].append(gh)
				# Episode time: the later start of the member's and the owner's
				# newest run of same-key failures, so a regroup after the owner
				# failed again (owner key K -> K2 -> K) is not hidden by the earlier
				# episode's marker, while the owner repeating K does not move it.
				episode_ts[gh] = max(items[gh]["newest_ts"], items[owner]["newest_ts"])
	for gh, it in items.items():
		if "grouped_under" in it["entry"] and gh not in release and gh not in grouped and gh not in held:
			release[gh] = "owner_changed"
	result["release"] = sorted(release)
	result["release_reasons"] = {str(gh): release[gh] for gh in sorted(release)}
	result["release_cooldown"] = [{"issue": gh, "key": cooldown[gh]} for gh in sorted(cooldown)]
	result["clear_cooldown"] = sorted(gh for gh in clear if gh not in cooldown)
	result["new"] = sorted(result["new"])
	result["newest_failure_ts"] = {str(gh): episode_ts[gh] for gh in result["new"]}
	return result, journal_seen


def cmd_group(args: argparse.Namespace) -> None:
	if not group_enabled():
		log_group("skipped", "switch_off")
		return
	if _aj is None:
		log_group("skipped", "helper_absent")
		return
	try:
		now = int(str(args.now).strip())
		wave_index = int(str(args.wave_index).strip())
	except ValueError:
		log_group("error", "args_invalid")
		return
	if now <= 0 or wave_index < 0:
		log_group("error", "args_invalid")
		return
	state = _read_json(args.state_file)
	if not isinstance(state, dict):
		log_group("error", "state_unreadable")
		return
	details = _read_json(args.journals_json)
	window_hours = _env_hours("PERSIST_ORCH_GROUP_WINDOW_HOURS", GROUP_WINDOW_HOURS_DEFAULT)
	max_hours = _env_hours("PERSIST_ORCH_GROUP_MAX_HOURS", GROUP_MAX_HOURS_DEFAULT)
	result, journal_seen = group_failures(state, wave_index, details, args.trusted_login or "", now, window_hours, max_hours)
	if not _write(args.out, json.dumps(result, sort_keys=True) + "\n"):
		log_group("error", "out_unwritable")
		return
	if not journal_seen:
		log_group("skipped", "journal_absent", released=len(result["release"]))
		return
	log_group(
		"ok", "evaluated", groups=len(result["groups"]),
		grouped=sum(len(g["members"]) for g in result["groups"]), released=len(result["release"]),
	)


def wave_check_enabled() -> bool:
	# Exactly "true", the same rule as enabled() / group_enabled() and the poller.
	return os.environ.get("PERSIST_ORCH_WAVE_CHECK_ENABLED", "false") == "true"


def log_wave_check(outcome: str, reason: str, **extra: int) -> None:
	line = f"PERSIST wave_check outcome={outcome} reason={_TOKEN_UNSAFE_RE.sub('_', reason)[:60]}"
	for key, value in extra.items():
		line += f" {key}={int(value)}"
	print(line, file=sys.stderr)


def _check_run_pages(payload: Any) -> tuple[list[dict], int] | None:
	"""Check runs and the reported total_count, or None for a wrong shape."""
	pages = payload if isinstance(payload, list) else [payload]
	runs: list[dict] = []
	total = 0
	for page in pages:
		if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
			return None
		runs.extend(r for r in page["check_runs"] if isinstance(r, dict))
		count = page.get("total_count")
		if isinstance(count, int) and not isinstance(count, bool):
			total = max(total, count)
	return runs, total


def _wave_check_required(required: str | None) -> tuple[str, list[str]]:
	"""("all" | "none" | "list", names), the same sentinels as
	_pr_required_check_names_for_base: "*" every failure blocks, "" none does."""
	if required is None or required.strip() == "*":
		return "all", []
	names = [n.strip() for n in required.split(",") if n.strip()]
	if not required.strip() or not names:
		return "none", []
	return "list", names


def classify_check_runs(payload: Any, now: int, wait_since: str | None, max_wait: int, required: str | None = "*") -> dict:
	"""wave_check.v1 result for one page of check runs (see the module docstring)."""
	result: dict = {
		"schema": "wave_check.v1", "outcome": "unavailable", "failing": [], "pending": [], "log_job_id": None,
		"summary_text": "", "waited_minutes": 0, "max_wait_minutes": max_wait, "wait_since_valid": False, "truncated": False,
	}
	parsed = _check_run_pages(payload)
	if parsed is None:
		return result
	runs, total = parsed
	result["truncated"] = total > len(runs)
	# Same rule as scripts/pr_checks_lib.sh: a check run carries no workflow
	# identity (this repo has three jobs named `lint`), so a newer run of the
	# same name supersedes only a cancelled run. A failed or still-running
	# run keeps counting even when a newer same-name run exists.
	named = [r for r in runs if isinstance(r.get("name"), str)]
	def _key(r: dict) -> tuple[int, str]:
		return (_positive_int(r.get("id")) or 0, str(r.get("started_at") or ""))
	def _superseded(r: dict) -> bool:
		return r.get("conclusion") == "cancelled" and any(o is not r and o.get("name") == r.get("name") and _key(o) > _key(r) for o in named)
	kept = [r for r in named if not _superseded(r) and r.get("conclusion") not in WAVE_CHECK_IGNORED_CONCLUSIONS]
	kept.sort(key=lambda r: (str(r.get("name")), _positive_int(r.get("id")) or 0))
	# Required-set filter, as for the final integration merge: a pending run
	# always holds, a failed run blocks only when its name is required.
	mode, names = _wave_check_required(required)
	if mode == "none":
		failing: list[dict] = []
		pending: list[dict] = []
	else:
		failing = [
			r for r in kept
			if r.get("status") == "completed" and r.get("conclusion") != "success" and (mode == "all" or r.get("name") in names)
		]
		pending = [r for r in kept if r.get("status") != "completed"]
	result["failing"] = [
		{"name": str(r.get("name")), "conclusion": str(r.get("conclusion") if r.get("conclusion") is not None else "unknown")}
		for r in failing[:WAVE_CHECK_LIST_MAX]
	]
	result["pending"] = [str(r.get("name")) for r in pending[:WAVE_CHECK_LIST_MAX]]
	for run in failing:
		app = run.get("app") if isinstance(run.get("app"), dict) else {}
		if app.get("slug") == "github-actions":
			# For an Actions check run the check-run id is the job id.
			if result["log_job_id"] is None:
				result["log_job_id"] = _positive_int(run.get("id"))
		elif not result["summary_text"]:
			output = run.get("output") if isinstance(run.get("output"), dict) else {}
			summary = output.get("summary")
			result["summary_text"] = summary if isinstance(summary, str) else ""
	if failing:
		result["outcome"] = "red"
	elif pending:
		result["outcome"] = "pending"
	elif kept and result["truncated"]:
		# Runs past the first page were not read: never green on a partial list.
		result["outcome"] = "pending"
	elif kept:
		result["outcome"] = "green"
	else:
		result["outcome"] = "no_checks"
	since = _ts_epoch(wait_since)
	if since is not None and since <= now:
		result["wait_since_valid"] = True
		result["waited_minutes"] = (now - since) // 60
		if result["outcome"] in ("pending", "no_checks") and result["waited_minutes"] >= max_wait:
			result["outcome"] = "timeout" if result["outcome"] == "pending" else "no_checks_timeout"
	return result


def cmd_wave_check(args: argparse.Namespace) -> None:
	if not wave_check_enabled():
		log_wave_check("skipped", "switch_off")
		return
	try:
		now = int(str(args.now).strip())
	except ValueError:
		log_wave_check("error", "args_invalid")
		return
	if now <= 0:
		log_wave_check("error", "args_invalid")
		return
	max_wait = _env_hours("PERSIST_ORCH_WAVE_CHECK_MAX_WAIT_MINUTES", WAVE_CHECK_MAX_WAIT_MINUTES_DEFAULT)
	result = classify_check_runs(_read_json(args.check_runs_file), now, args.wait_since, max_wait, args.required_names)
	if not _write(args.out, json.dumps(result, sort_keys=True) + "\n"):
		log_wave_check("error", "out_unwritable")
		return
	log_wave_check("ok", result["outcome"], failing=len(result["failing"]), pending=len(result["pending"]))


def _wave_check_neutralise(text: str) -> list[str]:
	"""Data lines with control characters, comment delimiters and fence-like
	"=" runs removed, scrubbed, and prefixed UNTRUSTED_DATA."""
	lines: list[str] = []
	for raw in text.replace("\r\n", "\n").split("\n"):
		line = _CONTROL_RE.sub(" ", raw).replace("<!--", "").replace("-->", "")
		line = _WAVE_CHECK_EQ_RUN_RE.sub("= = =", line)
		if _aj is not None:
			line = _aj._scrub(line)
		lines.append(WAVE_CHECK_DATA_PREFIX + line)
	return lines


def _log_tail(path: str | None) -> str:
	if not path:
		return ""
	try:
		data = Path(path).read_bytes()[-WAVE_CHECK_LOG_MAX_BYTES:]
	except OSError:
		return ""
	# Cut at a UTF-8 boundary: drop leading continuation bytes.
	start = 0
	while start < len(data) and start < 4 and (data[start] & 0xC0) == 0x80:
		start += 1
	return data[start:].decode("utf-8", "replace").strip("\n")


def _unmet_items(state: Any, wave_index: int, details: Any, login: str) -> list[tuple[int, str, list[str]]]:
	"""Items of the wave whose newest trusted journal entry has unmet acceptance."""
	found: list[tuple[int, str, list[str]]] = []
	if _aj is None or not login or not isinstance(details, dict) or not isinstance(state, dict):
		return found
	waves = state.get("waves")
	if not isinstance(waves, list) or not 0 <= wave_index < len(waves) or not isinstance(waves[wave_index], dict):
		return found
	issues = waves[wave_index].get("issues")
	for entry in issues if isinstance(issues, list) else []:
		if not isinstance(entry, dict):
			continue
		gh = _positive_int(entry.get("github_issue"))
		cached = details.get(str(gh)) if gh is not None else None
		if not isinstance(cached, dict) or cached.get("comments_available") is not True or not isinstance(cached.get("comments"), list):
			continue
		_cid, entries = _aj.parse(cached["comments"], login)
		if not entries:
			continue
		# Newest by ts; on a tie the later entry wins, as sorted(...)[-1] did.
		newest = max(reversed(entries), key=lambda e: e.get("ts", ""))
		acc = newest.get("acceptance") if isinstance(newest.get("acceptance"), dict) else {}
		if acc.get("state") != "unmet":
			continue
		raw_unmet = acc.get("unmet") if isinstance(acc.get("unmet"), list) else []
		found.append((gh, _local_id(entry.get("id")), [str(u) for u in raw_unmet][:WAVE_CHECK_UNMET_MAX]))
	return found


def render_wave_check_block(result: dict, log_text: str, unmet: list[tuple[int, str, list[str]]]) -> str:
	outcome = result.get("outcome")
	reason = "integration PR checks failed" if outcome == "red" else "integration PR checks still pending after the wait"
	data: list[str] = _wave_check_neutralise(f"outcome: {outcome} ({reason})")
	for item in result.get("failing") or []:
		if isinstance(item, dict):
			data += _wave_check_neutralise(f"failing check: {item.get('name', '')} ({item.get('conclusion', '')})")
	if result.get("truncated") is True:
		data += _wave_check_neutralise("note: only the first page of check runs was read")
	if outcome == "timeout":
		waited = _positive_int(result.get("waited_minutes")) or 0
		for name in result.get("pending") or []:
			data += _wave_check_neutralise(f"still pending after {waited} minutes: {name}")
	for gh, local_id, strings in unmet:
		line = f"unmet done-checks: issue {gh} ({local_id})"
		if strings:
			line += ": " + "; ".join(" ".join(s.split()) for s in strings)
		data += _wave_check_neutralise(line)
	if log_text:
		data += _wave_check_neutralise("log tail:") + _wave_check_neutralise(log_text)
	else:
		summary = result.get("summary_text") if isinstance(result.get("summary_text"), str) else ""
		summary = summary.encode("utf-8")[:WAVE_CHECK_LOG_MAX_BYTES].decode("utf-8", "ignore").strip("\n")
		if summary:
			data += _wave_check_neutralise("check summary:") + _wave_check_neutralise(summary)
		else:
			data += _wave_check_neutralise("(log unavailable)")
	head = WAVE_CHECK_BEGIN + "\n"
	tail = WAVE_CHECK_END + "\n"
	text = head + "".join(line + "\n" for line in data) + tail
	if len(text.encode("utf-8")) <= WAVE_CHECK_BLOCK_MAX_BYTES:
		return text
	trailer = WAVE_CHECK_DATA_PREFIX + "- ... truncated\n"
	budget = WAVE_CHECK_BLOCK_MAX_BYTES - len((head + trailer + tail).encode("utf-8"))
	kept: list[str] = []
	for line in data:
		size = len((line + "\n").encode("utf-8"))
		if size > budget:
			break
		kept.append(line)
		budget -= size
	return head + "".join(line + "\n" for line in kept) + trailer + tail


def cmd_wave_check_block(args: argparse.Namespace) -> None:
	if not wave_check_enabled():
		log_wave_check("skipped", "switch_off")
		return
	result = _read_json(args.result_file)
	if not isinstance(result, dict) or result.get("schema") != "wave_check.v1":
		log_wave_check("error", "result_unreadable")
		return
	if result.get("outcome") not in ("red", "timeout"):
		log_wave_check("skipped", "not_failing")
		return
	try:
		wave_index = int(str(args.wave_index).strip())
	except ValueError:
		wave_index = -1
	unmet = _unmet_items(_read_json(args.state_file), wave_index, _read_json(args.journals_json), args.trusted_login or "")
	text = render_wave_check_block(result, _log_tail(args.log_file), unmet)
	if not _write(args.out, text):
		log_wave_check("error", "out_unwritable")
		return
	log_wave_check("ok", "block_rendered", unmet=len(unmet), bytes=len(text.encode("utf-8")))


def plan_path(description: str, repo_root: str | Path) -> str:
	try:
		root = Path(os.path.realpath(repo_root))
	except (OSError, ValueError):
		return ""
	# A GitHub blob URL names the repo-relative path after /blob/<ref>/.
	description = _BLOB_URL_PREFIX_RE.sub("", description)
	for match in _MD_PATH_RE.finditer(description):
		token = match.group(0)
		while token.startswith("./"):
			token = token[2:]
		candidate = safe_path(token)
		if not candidate:
			continue
		target = root / candidate
		try:
			real = Path(os.path.realpath(target))
		except (OSError, ValueError):
			continue
		# realpath equal to the lexical path means no component is a symlink.
		if real != Path(os.path.normpath(target)) or not real.is_file():
			continue
		if root != real and root not in real.parents:
			continue
		return candidate
	return ""


def cmd_plan_path(args: argparse.Namespace) -> None:
	try:
		text = Path(args.description_file).read_text(encoding="utf-8", errors="replace")
	except OSError:
		return
	found = plan_path(text, args.repo_root)
	if found:
		print(found)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Orchestrator context (PERSIST_ORCH_CONTEXT_ENABLED).")
	sub = parser.add_subparsers(dest="cmd", required=True)
	since = sub.add_parser("since-note")
	since.add_argument("--state-file", required=True)
	since.add_argument("--current-wave", required=True)
	since.add_argument("--details-file", default=None)
	since.add_argument("--trusted-login", default="")
	since.add_argument("--out", required=True)
	projects = sub.add_parser("open-projects")
	projects.add_argument("--repo", required=True)
	projects.add_argument("--out", required=True)
	plan = sub.add_parser("plan-path")
	plan.add_argument("--description-file", required=True)
	plan.add_argument("--repo-root", default=".")
	group = sub.add_parser("group")
	group.add_argument("--state-file", required=True)
	group.add_argument("--journals-json", default=None)
	group.add_argument("--now", required=True)
	group.add_argument("--wave-index", required=True)
	group.add_argument("--trusted-login", default="")
	group.add_argument("--out", required=True)
	wave = sub.add_parser("wave-check")
	wave.add_argument("--check-runs-file", required=True)
	wave.add_argument("--now", required=True)
	wave.add_argument("--wait-since", default=None)
	wave.add_argument("--required-names", default="*")
	wave.add_argument("--out", required=True)
	block = sub.add_parser("wave-check-block")
	block.add_argument("--result-file", required=True)
	block.add_argument("--log-file", default=None)
	block.add_argument("--state-file", required=True)
	block.add_argument("--wave-index", required=True)
	block.add_argument("--journals-json", default=None)
	block.add_argument("--trusted-login", default="")
	block.add_argument("--out", required=True)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	handlers = {
		"since-note": cmd_since_note, "open-projects": cmd_open_projects, "plan-path": cmd_plan_path, "group": cmd_group,
		"wave-check": cmd_wave_check, "wave-check-block": cmd_wave_check_block,
	}
	try:
		handlers[args.cmd](args)
	except Exception as exc:  # noqa: BLE001 - fail open, never block a tick or the decomposer
		if args.cmd in ("wave-check", "wave-check-block"):
			log_wave_check("error", "exception_" + type(exc).__name__)
		else:
			(log_group if args.cmd == "group" else log)("error", "exception_" + type(exc).__name__)
	return 0


if __name__ == "__main__":
	sys.exit(main())
