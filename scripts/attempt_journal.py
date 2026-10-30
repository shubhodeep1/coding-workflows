#!/usr/bin/env python3
"""Attempt journal (issue #8428, phase P1 of
docs/plans/persistent-unattended-workflows-plan.md).

One comment per issue (or per pull request when no issue is linked) records
every unattended stage's attempt: clarify, plan, implement, review, resolver.
The comment's visible part is a short table; the machine record sits in an
HTML-comment marker, the same shape as AI_STANDALONE_STALL_STATE_V1:

	<!-- AI_ATTEMPT_JOURNAL_V1
	{"schema":"attempt_journal.v1","entries":[...]}
	AI_ATTEMPT_JOURNAL_V1 -->

Trust: only the newest marker comment authored by the pipeline login is read.
An unknown login trusts nothing. Every entry field except ``note`` is computed
by the host; ``note`` is model text (capped at 300 characters) and is always
rendered as UNTRUSTED_DATA.

Switch: PERSIST_JOURNAL_ENABLED (default ``false``, defaulted here per the
unattended instructions section 8). With it off every subcommand logs
``PERSIST journal outcome=skipped reason=disabled`` and exits 0 without any
API call or output file change.

Fail-open: every CLI path exits 0. Errors log
``PERSIST journal outcome=error reason=<r>`` to stderr.

CLI:
	append --repo R (--issue N | --pr N [--head-ref B]) --stage S --outcome O [--approach N|inherit]
		[--attempt A] [--note-file F] [--files-file F] [--gate-result F]
		[--deletions-from-commit] [--failure-workflow W --failure-step S
		--failure-text-file F] [--comments-file F] [--trusted-login L]
	render [--repo R (--issue N | --pr N [--head-ref B]) | --comments-file F] --out F
		[--trusted-login L] [--max-bytes N]
	latest-key --comments-file F [--trusted-login L]

API budget (section 14): ``append`` makes at most 1 paginated comments read
(none with --comments-file) and 1 write (PATCH of the existing journal comment
or POST of a new one); ``render`` makes at most 1 comments read; both add at
most 1 ``gh api user`` identity lookup when --trusted-login is absent (cached
in ${RUNTIME_DIR}/attempt_journal_login.txt). Audited existing calls: the
callers' own comment reads (unblock_judge.sh item_comments.json, the poller's
issue_comments_json and per-tick GraphQL caches) are reused through
--comments-file wherever the caller already holds them; the remaining stages
(clarify, plan, implement, review, resolver) hold no comments list of the
journal's issue, so one read is the minimum.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MARKER = "AI_ATTEMPT_JOURNAL_V1"
SCHEMA = "attempt_journal.v1"
MAX_ENTRIES = 40
NOTE_MAX = 300
FILES_MAX = 20
DELETIONS_MAX = 10
UNMET_MAX = 10
VISIBLE_MAX = 10
# Journal comments start with this header (build_comment); a pipeline-authored
# comment that merely contains a marker block (a plan echoing the journal) is
# never treated as, or overwritten as, the journal.
HEADER = "### AI attempt journal"
# GitHub rejects comment bodies over 65,536 characters; stay well below it.
COMMENT_MAX_CHARS = 60000
STAGES = ("clarify", "plan", "implement", "review", "resolver", "postmerge", "governor", "wave_check")
OUTCOMES = ("ok", "fail", "partial", "blocked", "held", "unverified")
ACCEPTANCE_STATES = ("met", "unmet", "invalid", "none")
GATE_STATUSES = ("pass", "fail", "unverified", "skipped")
BEGIN_FENCE = "=== BEGIN UNTRUSTED ATTEMPT JOURNAL (host-recorded; note fields are model text) ==="
END_FENCE = "=== END UNTRUSTED ATTEMPT JOURNAL ==="
WORD_JOINER = "\u2060"
_MARKER_RE = re.compile(r"<!--\s*" + MARKER + r"\s*\n(.*?)\n\s*" + MARKER + r"\s*-->", re.S)
_FENCE_FORGERY_RE = re.compile(r"={3}(\s*(BEGIN|END)\s+UNTRUSTED)", re.I)
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HEX12_RE = re.compile(r"^[0-9a-f]{12}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.:/-]")


def log(outcome: str, reason: str) -> None:
	print(f"PERSIST journal outcome={outcome} reason={reason}", file=sys.stderr)


def enabled() -> bool:
	return os.environ.get("PERSIST_JOURNAL_ENABLED", "false").strip().lower() == "true"


def now_ts() -> str:
	return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token(value: Any, limit: int = 80) -> str:
	return _SAFE_TOKEN_RE.sub("_", str(value if value is not None else ""))[:limit]


def _int_or_none(value: Any) -> int | None:
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value if value >= 0 else None
	if isinstance(value, str) and value.isdigit():
		return int(value)
	return None


def _path(value: Any) -> str | None:
	if not isinstance(value, str) or not value or len(value) > 300:
		return None
	if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
		return None
	return value


def normalise_entry(raw: Any) -> dict | None:
	"""Return a schema-valid entry, or None when stage or outcome is unknown."""
	if not isinstance(raw, dict):
		return None
	stage = raw.get("stage")
	outcome = raw.get("outcome")
	if stage not in STAGES or outcome not in OUTCOMES:
		return None
	ts = raw.get("ts")
	if not isinstance(ts, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts):
		return None
	head_sha = raw.get("head_sha")
	failure_key = raw.get("failure_key")
	evidence_hash = raw.get("evidence_hash")
	files = [p for p in (raw.get("files_changed") or []) if _path(p)] if isinstance(raw.get("files_changed"), list) else []
	gate_raw = raw.get("gate") if isinstance(raw.get("gate"), dict) else {}
	gate = {
		"status": gate_raw.get("status") if gate_raw.get("status") in GATE_STATUSES else None,
		"pr_caused": _int_or_none(gate_raw.get("pr_caused")),
		"inherited": _int_or_none(gate_raw.get("inherited")),
		"unverified": _int_or_none(gate_raw.get("unverified")),
	}
	acc_raw = raw.get("acceptance") if isinstance(raw.get("acceptance"), dict) else {}
	unmet = acc_raw.get("unmet") if isinstance(acc_raw.get("unmet"), list) else []
	acceptance = {
		"state": acc_raw.get("state") if acc_raw.get("state") in ACCEPTANCE_STATES else "none",
		"unmet": [str(u)[:200] for u in unmet if isinstance(u, (str, int))][:UNMET_MAX],
	}
	deletions = []
	for item in (raw.get("editor_deletions") or []) if isinstance(raw.get("editor_deletions"), list) else []:
		if isinstance(item, dict) and _path(item.get("file")) and _int_or_none(item.get("lines")) is not None:
			deletions.append({"file": item["file"], "lines": _int_or_none(item.get("lines"))})
	note = raw.get("note") if isinstance(raw.get("note"), str) else ""
	approach = _int_or_none(raw.get("approach"))
	return {
		"ts": ts,
		"stage": stage,
		"run_id": _token(raw.get("run_id")),
		"attempt": _token(raw.get("attempt")),
		"head_sha": head_sha if isinstance(head_sha, str) and _SHA_RE.match(head_sha) else "",
		"approach": approach if approach is not None else 0,
		"outcome": outcome,
		"failure_key": failure_key if isinstance(failure_key, str) and _HEX12_RE.match(failure_key) else "",
		"files_changed": files[:FILES_MAX],
		"gate": gate,
		"acceptance": acceptance,
		"editor_deletions": deletions[:DELETIONS_MAX],
		"evidence_hash": evidence_hash if isinstance(evidence_hash, str) and _HASH_RE.match(evidence_hash) else "",
		"note": note[:NOTE_MAX],
	}


def _norm_login(login: Any) -> str:
	if not isinstance(login, str):
		return ""
	login = login.strip().casefold()
	if login.endswith("[bot]"):
		login = login[: -len("[bot]")]
	return login


def _comment_author(comment: dict) -> str:
	for key in ("user", "author"):
		value = comment.get(key)
		if isinstance(value, dict) and isinstance(value.get("login"), str):
			return value["login"]
	return ""


def _comment_id(comment: dict) -> int | None:
	for key in ("id", "databaseId"):
		value = comment.get(key)
		if isinstance(value, int) and not isinstance(value, bool) and value > 0:
			return value
	return None


def parse(comments: Any, trusted_login: str) -> tuple[int | None, list[dict]]:
	"""Newest trusted journal comment's id and entries (REST or GraphQL shape)."""
	trusted = _norm_login(trusted_login)
	if not trusted or not isinstance(comments, list):
		return None, []
	for comment in reversed(comments):
		if not isinstance(comment, dict):
			continue
		body = comment.get("body")
		if not isinstance(body, str) or ("<!-- " + MARKER) not in body:
			continue
		if not body.lstrip("\ufeff").startswith(HEADER + "\n"):
			continue
		if _norm_login(_comment_author(comment)) != trusted:
			continue
		match = _MARKER_RE.search(body)
		if not match:
			continue
		try:
			data = json.loads(match.group(1))
		except ValueError:
			continue
		if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("entries"), list):
			continue
		entries = [e for e in (normalise_entry(raw) for raw in data["entries"]) if e]
		return _comment_id(comment), merge(entries, [])
	return None, []


def _key(entry: dict) -> tuple[str, str, str]:
	return (entry.get("stage", ""), entry.get("run_id", ""), entry.get("attempt", ""))


def merge(entries: list[dict], new: list[dict]) -> list[dict]:
	"""Merge on (stage, run_id, attempt): new replaces old; newest MAX_ENTRIES kept."""
	by_key: dict[tuple[str, str, str], dict] = {}
	for entry in list(entries) + list(new):
		if isinstance(entry, dict):
			by_key[_key(entry)] = entry
	ordered = sorted(by_key.values(), key=lambda e: e.get("ts", ""))
	return ordered[-MAX_ENTRIES:]


def latest_key(entries: list[dict]) -> str:
	if not entries:
		return ""
	newest = sorted(entries, key=lambda e: e.get("ts", ""))[-1]
	return "|".join(_key(newest))


def _scrub(text: str) -> str:
	"""Same rules as the gate scripts' neutralise/scrub plus fence breaking."""
	text = _FENCE_FORGERY_RE.sub(r"= = =\1", text)
	text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
	text = re.sub(r"#(\d)", "#" + WORD_JOINER + r"\1", text)
	text = re.sub(r"@(?=[A-Za-z0-9_-])", "@" + WORD_JOINER, text)
	lines = []
	for line in text.split("\n"):
		stripped = line.lstrip()
		lines.append((" " + line) if stripped.startswith("::") or stripped.startswith("##[") else line)
	return "\n".join(lines)


def _cell(value: Any) -> str:
	text = " ".join(str(value if value is not None else "-").split())
	return _scrub(text.replace("|", "/")) or "-"


def _gate_cell(gate: dict) -> str:
	if not isinstance(gate, dict) or not gate.get("status"):
		return "-"
	return "{} pr_caused={} inherited={} unverified={}".format(
		gate.get("status"), gate.get("pr_caused"), gate.get("inherited"), gate.get("unverified"))


def _entry_lines(entry: dict) -> list[str]:
	acc = entry.get("acceptance") or {}
	acc_text = acc.get("state", "none")
	if acc.get("unmet"):
		acc_text += " unmet=" + ", ".join(str(u) for u in acc["unmet"])
	files = entry.get("files_changed") or []
	row = " | ".join(_cell(v) for v in (
		entry.get("ts"), entry.get("stage"), f"{entry.get('run_id')}/{entry.get('attempt')}", entry.get("outcome"),
		entry.get("approach"), entry.get("failure_key") or "-", _gate_cell(entry.get("gate") or {}), acc_text,
		", ".join(files) if files else "-",
	))
	lines = [row]
	deletions = entry.get("editor_deletions") or []
	if deletions:
		lines.append("  editor_deletions: " + _cell(", ".join(f"{d['file']} (-{d['lines']})" for d in deletions)))
	if entry.get("note"):
		lines.append("UNTRUSTED_DATA: " + _cell(entry["note"]))
	return lines


def render(entries: list[dict], max_bytes: int = 6000) -> str:
	"""Fenced compact table, newest first; truncated on entry boundaries."""
	if not entries:
		return ""
	head = [BEGIN_FENCE, "ts | stage | run/attempt | outcome | approach | failure_key | gate | acceptance | files_changed"]
	tail = [END_FENCE]
	budget = max_bytes - len("\n".join(head + tail).encode("utf-8")) - 2
	body: list[str] = []
	ordered = sorted(entries, key=lambda e: e.get("ts", ""), reverse=True)
	omitted = 0
	for index, entry in enumerate(ordered):
		block = _entry_lines(entry)
		size = len("\n".join(block).encode("utf-8")) + 1
		reserve = 48 if index < len(ordered) - 1 else 0
		if size + reserve > budget:
			omitted = len(ordered) - index
			break
		body.extend(block)
		budget -= size
	if omitted:
		body.append(f"({omitted} older entries omitted)")
	if not body:
		return ""
	return "\n".join(head + body + tail) + "\n"


def _marker_json(entries: list[dict]) -> str:
	text = json.dumps({"schema": SCHEMA, "entries": entries}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
	# Model text can never close the HTML comment: no literal < or > remains.
	return text.replace("<", "\\u003c").replace(">", "\\u003e")


def build_comment(entries: list[dict]) -> str:
	lines = [HEADER, "",
		"Recorded by the unattended pipeline; read by the next stage and the judges.", "",
		"| ts | stage | outcome | approach | gate | files |", "|---|---|---|---|---|---|"]
	for entry in sorted(entries, key=lambda e: e.get("ts", ""), reverse=True)[:VISIBLE_MAX]:
		gate = (entry.get("gate") or {}).get("status") or "-"
		lines.append("| {} | {} | {} | {} | {} | {} |".format(
			_cell(entry.get("ts")), _cell(entry.get("stage")), _cell(entry.get("outcome")),
			_cell(entry.get("approach")), _cell(gate), len(entry.get("files_changed") or [])))
	lines += ["", f"<!-- {MARKER}", _marker_json(entries), f"{MARKER} -->", ""]
	return "\n".join(lines)


def failure_key(workflow: str, step: str, text: str) -> str:
	try:
		sys.path.insert(0, str(Path(__file__).resolve().parent))
		import workflow_failure_heal  # noqa: PLC0415
		value = workflow_failure_heal.fingerprint(workflow, step, workflow_failure_heal.error_signature(text))
		value = str(value).lower()
		return value[:12] if re.match(r"^[0-9a-f]{12}", value) else ""
	except Exception:  # noqa: BLE001 - advisory field, fail open
		return ""


# --- host side (git, gh) ------------------------------------------------------

def _gh(args: list[str]) -> bytes | None:
	"""One logical gh api call through gh_api_retry (optional: honours the breaker)."""
	try:
		sys.path.insert(0, str(Path(__file__).resolve().parent))
		import gh_api_retry  # noqa: PLC0415
	except Exception:  # noqa: BLE001
		gh_api_retry = None
		log("error", "gh_api_retry_unavailable")
	if gh_api_retry is not None:
		result = gh_api_retry.call(args, optional=True)
		return result.stdout if result.rc == 0 else None
	try:
		proc = subprocess.run(["gh", "api", *args], capture_output=True, check=False, timeout=120)
	except (OSError, subprocess.SubprocessError):
		return None
	return proc.stdout if proc.returncode == 0 else None


def _decode_pages(raw: bytes) -> list | None:
	"""`gh api --paginate` concatenates one JSON array per page."""
	text = raw.decode("utf-8", "replace").strip()
	decoder = json.JSONDecoder()
	out: list = []
	index = 0
	while index < len(text):
		try:
			value, end = decoder.raw_decode(text, index)
		except ValueError:
			return None
		if not isinstance(value, list):
			return None
		out.extend(value)
		index = end
		while index < len(text) and text[index].isspace():
			index += 1
	return out


def resolve_login(explicit: str | None) -> str:
	if explicit:
		return explicit.strip()
	cache = None
	runtime = os.environ.get("RUNTIME_DIR", "")
	if runtime and os.path.isdir(runtime):
		cache = Path(runtime) / "attempt_journal_login.txt"
		try:
			cached = cache.read_text(encoding="utf-8").strip()
			if cached:
				return cached
		except OSError:
			pass
	raw = _gh(["user"])
	login = ""
	if raw:
		try:
			data = json.loads(raw.decode("utf-8", "replace"))
			login = data.get("login", "") if isinstance(data, dict) else ""
		except ValueError:
			login = ""
	if login and cache is not None:
		try:
			cache.write_text(login + "\n", encoding="utf-8")
		except OSError:
			pass
	return login if isinstance(login, str) else ""


def load_comments(path: str | None, repo: str | None, issue: str | None) -> list | None:
	if path:
		try:
			data = json.loads(Path(path).read_text(encoding="utf-8"))
		except (OSError, ValueError):
			return None
		if isinstance(data, dict):
			nodes = data.get("comments", {}).get("nodes") if isinstance(data.get("comments"), dict) else data.get("nodes")
			data = nodes
		return data if isinstance(data, list) else None
	if not repo or not issue:
		return None
	raw = _gh(["--paginate", f"repos/{repo}/issues/{issue}/comments?per_page=100"])
	return _decode_pages(raw) if raw is not None else None


def _git(*args: str) -> str:
	try:
		proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False, timeout=60)
	except (OSError, subprocess.SubprocessError):
		return ""
	return proc.stdout if proc.returncode == 0 else ""


def gate_fields(path: str | None) -> dict:
	gate = {"status": None, "pr_caused": None, "inherited": None, "unverified": None}
	if not path:
		return gate
	try:
		data = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return gate
	if not isinstance(data, dict):
		return gate
	gate["status"] = data.get("status") if data.get("status") in GATE_STATUSES else "unverified"
	for key, name in (("failures", "pr_caused"), ("inherited", "inherited"), ("unverified", "unverified")):
		value = data.get(key)
		gate[name] = len(value) if isinstance(value, list) else None
	return gate


def commit_deletions() -> list[dict]:
	out = []
	for line in _git("diff", "--numstat", "HEAD~1", "HEAD").splitlines():
		parts = line.split("\t", 2)
		if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) > 0:
			out.append({"file": parts[2], "lines": int(parts[1])})
	out.sort(key=lambda d: d["lines"], reverse=True)
	return out[:DELETIONS_MAX]


def _read_lines(path: str | None) -> list[str]:
	if not path:
		return []
	try:
		return [line.strip() for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
	except OSError:
		return []


def _valid_target(repo: str | None, issue: str | None) -> bool:
	return bool(repo and _REPO_RE.match(repo) and issue and issue.isdigit() and int(issue) > 0)


def resolve_target(issue: str | None, pr: str | None, head_ref: str | None) -> str:
	"""Journal issue: --issue, else the `ai/issue-N` head, else the first linked
	issue (LINKED_ISSUES_JSON, then LINKED_ISSUE_FALLBACK_NUMBERS_JSON, as
	review_apply_fixes.sh's first_linked_issue_number), else the PR itself."""
	if issue and issue.isdigit():
		return issue
	match = re.match(r"^ai/issue-([1-9][0-9]*)$", head_ref or "")
	if match:
		return match.group(1)
	for name, key in (("LINKED_ISSUES_JSON", "number"), ("LINKED_ISSUE_FALLBACK_NUMBERS_JSON", None)):
		try:
			payload = json.loads(os.environ.get(name, "[]") or "[]")
		except ValueError:
			continue
		if isinstance(payload, list) and payload:
			first = payload[0].get(key) if key and isinstance(payload[0], dict) else (None if key else payload[0])
			if isinstance(first, int) and not isinstance(first, bool) and first > 0:
				return str(first)
			if isinstance(first, str) and first.isdigit() and int(first) > 0:
				return first
	return pr if pr and pr.isdigit() else ""


def cmd_append(args: argparse.Namespace) -> None:
	args.issue = resolve_target(args.issue, args.pr, args.head_ref)
	if not _valid_target(args.repo, args.issue):
		log("error", "invalid_target")
		return
	if args.stage not in STAGES or args.outcome not in OUTCOMES:
		log("error", "invalid_entry")
		return
	login = resolve_login(args.trusted_login)
	if not login:
		log("error", "identity_unavailable")
		return
	comments = load_comments(args.comments_file, args.repo, args.issue)
	if comments is None:
		log("error", "gh_read")
		return
	comment_id, entries = parse(comments, login)
	if args.approach == "inherit":
		plans = [e for e in entries if e.get("stage") == "plan"]
		approach = plans[-1]["approach"] if plans else 0
	else:
		approach = _int_or_none(args.approach) or 0
	note = ""
	if args.note_file:
		try:
			note = Path(args.note_file).read_text(encoding="utf-8", errors="replace")[:NOTE_MAX]
		except OSError:
			note = ""
	fkey = ""
	if args.failure_workflow and args.failure_text_file:
		try:
			text = Path(args.failure_text_file).read_text(encoding="utf-8", errors="replace")
		except OSError:
			text = ""
		fkey = failure_key(args.failure_workflow, args.failure_step or "", text) if text else ""
	entry = normalise_entry({
		"ts": now_ts(),
		"stage": args.stage,
		"run_id": os.environ.get("GITHUB_RUN_ID", "local"),
		"attempt": args.attempt or os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
		"head_sha": _git("rev-parse", "HEAD").strip(),
		"approach": approach,
		"outcome": args.outcome,
		"failure_key": fkey,
		"files_changed": _read_lines(args.files_file),
		"gate": gate_fields(args.gate_result),
		"acceptance": {"state": "none", "unmet": []},
		"editor_deletions": commit_deletions() if args.deletions_from_commit else [],
		"evidence_hash": "",
		"note": note,
	})
	if entry is None:
		log("error", "invalid_entry")
		return
	merged = merge(entries, [entry])
	# Drop the oldest entries until the comment fits GitHub's body limit, so an
	# oversized record never blocks every later append.
	while len(merged) > 1 and len(build_comment(merged)) > COMMENT_MAX_CHARS:
		merged = merged[1:]
	runtime = os.environ.get("RUNTIME_DIR", "")
	base = Path(runtime) if runtime and os.path.isdir(runtime) else Path(os.environ.get("RUNNER_TEMP", "/tmp"))
	body_file = base / f"attempt_journal_body.{os.getpid()}.md"
	try:
		body_file.write_text(build_comment(merged), encoding="utf-8")
		if comment_id:
			raw = _gh([f"repos/{args.repo}/issues/comments/{comment_id}", "-X", "PATCH", "-F", f"body=@{body_file}"])
		else:
			raw = _gh([f"repos/{args.repo}/issues/{args.issue}/comments", "-X", "POST", "-F", f"body=@{body_file}"])
	finally:
		try:
			body_file.unlink()
		except OSError:
			pass
	if raw is None:
		log("error", "gh_write")
		return
	log("ok", f"appended stage={args.stage} entries={len(merged)}")


def _write_out(path: str, text: str) -> None:
	try:
		Path(path).write_text(text, encoding="utf-8")
	except OSError:
		log("error", "out_unwritable")


def cmd_render(args: argparse.Namespace) -> None:
	_write_out(args.out, "")
	args.issue = resolve_target(args.issue, args.pr, args.head_ref)
	if not args.comments_file and not _valid_target(args.repo, args.issue):
		log("error", "invalid_target")
		return
	login = resolve_login(args.trusted_login)
	if not login:
		log("error", "identity_unavailable")
		return
	comments = load_comments(args.comments_file, args.repo, args.issue)
	if comments is None:
		log("error", "gh_read")
		return
	_, entries = parse(comments, login)
	text = render(entries, max(512, args.max_bytes))
	_write_out(args.out, text)
	log("ok", f"rendered entries={len(entries)}")


def cmd_latest_key(args: argparse.Namespace) -> None:
	login = (args.trusted_login or "").strip()
	comments = load_comments(args.comments_file, None, None)
	if not login or comments is None:
		return
	_, entries = parse(comments, login)
	key = latest_key(entries)
	if key:
		print(key)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Attempt journal (AI_ATTEMPT_JOURNAL_V1).")
	sub = parser.add_subparsers(dest="cmd", required=True)
	append = sub.add_parser("append")
	append.add_argument("--repo", required=True)
	append.add_argument("--issue", default="")
	append.add_argument("--pr", default="")
	append.add_argument("--head-ref", default="")
	append.add_argument("--stage", required=True)
	append.add_argument("--outcome", required=True)
	append.add_argument("--approach", default="inherit")
	append.add_argument("--attempt", default="")
	append.add_argument("--note-file")
	append.add_argument("--files-file")
	append.add_argument("--gate-result")
	append.add_argument("--deletions-from-commit", action="store_true")
	append.add_argument("--failure-workflow")
	append.add_argument("--failure-step")
	append.add_argument("--failure-text-file")
	append.add_argument("--comments-file")
	append.add_argument("--trusted-login")
	rend = sub.add_parser("render")
	rend.add_argument("--repo")
	rend.add_argument("--issue", default="")
	rend.add_argument("--pr", default="")
	rend.add_argument("--head-ref", default="")
	rend.add_argument("--out", required=True)
	rend.add_argument("--comments-file")
	rend.add_argument("--trusted-login")
	rend.add_argument("--max-bytes", type=int, default=6000)
	latest = sub.add_parser("latest-key")
	latest.add_argument("--comments-file", required=True)
	latest.add_argument("--trusted-login")
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
		{"append": cmd_append, "render": cmd_render, "latest-key": cmd_latest_key}[args.cmd](args)
	except Exception as exc:  # noqa: BLE001 - fail open, never block a stage
		log("error", "exception_" + type(exc).__name__)
	return 0


if __name__ == "__main__":
	sys.exit(main())
