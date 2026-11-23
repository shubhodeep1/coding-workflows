#!/usr/bin/env python3
"""Build the thread-resolution plan for review_resolve_review_threads.sh.

Separated from the shell driver so the mapping rules — the part that
decides whether a reviewer's comment gets marked resolved — are unit
testable without a GitHub token or a live PR.

Inputs (environment):
  EDITOR_SUMMARY_FILE           editor summary carrying "PR comment audit:"
  PR_ALL_COMMENTS_CONTEXT_FILE  entry[N].<field> dump written by
                                scripts/review_collect_pr_metadata.sh
  THREADS_JSON                  [{thread_id, is_resolved, comment_ids, path,
                                author, reply_comment_id}] distilled from the
                                GraphQL query;
                                comment_ids covers every comment in the
                                thread, replies included; reply_comment_id is
                                the top-level comment accepted by the reply API
  REVIEW_APPLIED_CHANGES_PERSISTED
                                false skips "applied" dispositions
  PLAN_FILE                     JSONL output path
  MAX_THREADS                   cap on plan length (default 50)

Output: one JSON object per line on PLAN_FILE, each with thread_id,
comment_id, reply_comment_id, disposition, reason, and path. Skipped
entries are reported on stderr with the reason, never silently dropped.

Exit code is 0 whenever a plan could be written (including an empty
plan); 1 only on unreadable required inputs, which the caller treats as
fail-open.
"""

from __future__ import annotations

import json
import os
import re
import sys

SECTION_HEADER = re.compile(r"^[ \t]*[A-Za-z][A-Za-z ()/-]*:[ \t]*$")
AUDIT_HEADER = re.compile(r"^[ \t]*PR comment audit:[ \t]*$")
ENTRY_INDEX = re.compile(r"entry\s*\[\s*(\d+)\s*\]")
CONTEXT_FIELD = re.compile(r"^entry\[(\d+)\]\.([A-Za-z_]+):[ \t]?(.*)$")
BACKTICKED = re.compile(r"`([^`]+)`")
BARE_PATH = re.compile(r"(?<![`\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+)(?::\d+)?")
DISPOSITION_SEPARATOR = re.compile(r"\s+[—–]\s+")
DISPOSITION_LABEL = re.compile(r"\bdisposition\s*:\s*", re.I)
REASON_MAX_CHARS = 500

# Checked in order: "already applied" must not be read as "applied", and
# "not applied" must not be read as "applied" either.
DISPOSITION_PATTERNS = (
	("already satisfied", re.compile(r"already[\s-]+(?:satisfied|applied|present|fixed|addressed)\b", re.I)),
	("ignored", re.compile(r"(?:ignored|rejected|not[\s-]+(?:applied|fixed|addressed)|no[\s-]+action|skipped)\b", re.I)),
	("applied", re.compile(r"(?:applied|fixed|addressed)\b", re.I)),
)


def warn(message: str) -> None:
	sys.stderr.write(f"::warning::review_resolve_review_threads: {message}\n")


def note(message: str) -> None:
	sys.stderr.write(f"review_resolve_review_threads: {message}\n")


def read_text(path: str) -> str:
	with open(path, "r", encoding="utf-8", errors="replace") as handle:
		return handle.read()


def extract_audit_section(summary_text: str) -> list[str]:
	"""Return the bullet lines under 'PR comment audit:'."""
	lines = summary_text.splitlines()
	collected: list[str] = []
	in_section = False
	for line in lines:
		if AUDIT_HEADER.match(line):
			in_section = True
			continue
		if in_section:
			if SECTION_HEADER.match(line):
				break
			collected.append(line)
	return collected


def normalize_path(value: str) -> str:
	value = value.strip().strip("`").strip()
	value = re.sub(r":\d+(?:-\d+)?$", "", value)
	return value.lstrip("./")


def extract_path(line: str) -> str:
	for candidate in BACKTICKED.findall(line):
		normalized = normalize_path(candidate)
		if "/" in normalized or "." in normalized:
			return normalized
	match = BARE_PATH.search(line)
	if match:
		return normalize_path(match.group(1))
	return ""


def find_disposition(line: str) -> tuple[str, int]:
	"""Return the first explicit disposition field and its end offset."""
	candidate_starts = [0]
	candidate_starts.extend(match.end() for match in DISPOSITION_SEPARATOR.finditer(line))
	candidate_starts.extend(match.start() for match in DISPOSITION_LABEL.finditer(line))
	for candidate_start in sorted(set(candidate_starts)):
		candidate_text = line[candidate_start:]
		leading_length = len(candidate_text) - len(candidate_text.lstrip())
		candidate_text = candidate_text.lstrip()
		if candidate_text.lower().startswith("disposition"):
			label_match = DISPOSITION_LABEL.match(candidate_text)
			if label_match is not None:
				leading_length += label_match.end()
				candidate_text = candidate_text[label_match.end():]
		for name, pattern in DISPOSITION_PATTERNS:
			match = pattern.match(candidate_text)
			if match is not None:
				return name, candidate_start + leading_length + match.end()
	return "", -1


def extract_disposition(line: str) -> str:
	disposition, _ = find_disposition(line)
	return disposition


def extract_reason(line: str, disposition: str) -> str:
	"""Text following the disposition token, trimmed for use as a reply."""
	matched_disposition, match_end = find_disposition(line)
	if matched_disposition == disposition and match_end >= 0:
		tail = line[match_end:].strip().lstrip(";:,-—– ").strip()
		if tail:
			if len(tail) > REASON_MAX_CHARS:
				return tail[: REASON_MAX_CHARS - 3].rstrip() + "..."
			return tail
	return ""


def parse_audit_entries(summary_text: str) -> list[dict]:
	entries: list[dict] = []
	seen_indices: set[int] = set()
	for line in extract_audit_section(summary_text):
		stripped = line.strip()
		if not stripped.startswith("-"):
			continue
		body = stripped[1:].strip()
		if not body or body.lower().startswith("none"):
			continue
		index_match = ENTRY_INDEX.search(body)
		if index_match is None:
			warn(f"audit line has no entry[N] marker; skipping: {body[:120]}")
			continue
		index = int(index_match.group(1))
		if index in seen_indices:
			warn(f"entry[{index}] audited more than once; keeping the first occurrence")
			continue
		disposition = extract_disposition(body)
		if not disposition:
			warn(f"entry[{index}] has no recognisable disposition; skipping")
			continue
		seen_indices.add(index)
		entries.append(
			{
				"index": index,
				"disposition": disposition,
				"reason": extract_reason(body, disposition),
				"audit_path": extract_path(body),
			}
		)
	return entries


def parse_context_entries(context_text: str) -> dict[int, dict]:
	entries: dict[int, dict] = {}
	for line in context_text.splitlines():
		match = CONTEXT_FIELD.match(line)
		if match is None:
			continue
		index = int(match.group(1))
		field = match.group(2)
		# First occurrence wins. review_collect_pr_metadata.sh emits the
		# structured fields before entry[N].body, and a comment body is
		# attacker-controlled prose that may itself contain a line
		# shaped like "entry[0].id: 999". Honouring a later duplicate
		# would let comment text redirect a resolve at another thread.
		entries.setdefault(index, {}).setdefault(field, match.group(3).strip())
	return entries


def paths_agree(audit_path: str, context_path: str) -> bool:
	"""True when the audit line's path is consistent with the real comment.

	A disagreement is the signature of the editor auditing one comment
	while quoting another's location, so it disqualifies the entry.
	"""
	if not audit_path or not context_path:
		return True
	left = normalize_path(audit_path)
	right = normalize_path(context_path)
	if left == right:
		return True
	return left.endswith("/" + right) or right.endswith("/" + left)


def build_plan() -> int:
	summary_file = os.environ.get("EDITOR_SUMMARY_FILE", "")
	context_file = os.environ.get("PR_ALL_COMMENTS_CONTEXT_FILE", "")
	threads_file = os.environ.get("THREADS_JSON", "")
	plan_file = os.environ.get("PLAN_FILE", "")

	if not plan_file:
		warn("PLAN_FILE is unset")
		return 1

	try:
		summary_text = read_text(summary_file)
		context_text = read_text(context_file)
		threads = json.loads(read_text(threads_file) or "[]")
	except (OSError, ValueError) as error:
		warn(f"could not read plan inputs ({error})")
		return 1

	if not isinstance(threads, list):
		warn("thread payload was not a list")
		return 1

	try:
		max_threads = int(os.environ.get("MAX_THREADS", "50"))
	except ValueError:
		max_threads = 50
	if max_threads <= 0:
		max_threads = 50
	applied_changes_persisted = os.environ.get("REVIEW_APPLIED_CHANGES_PERSISTED", "false").lower() == "true"

	# Key every comment in a thread, not just its anchor: an audited id
	# may belong to a reply, because GET /pulls/<n>/comments returns
	# replies in the same flat list the context file is built from.
	threads_by_comment: dict[int, dict] = {}
	for thread in threads:
		if not isinstance(thread, dict):
			continue
		for comment_id in thread.get("comment_ids") or []:
			if isinstance(comment_id, int):
				threads_by_comment.setdefault(comment_id, thread)

	audit_entries = parse_audit_entries(summary_text)
	context_entries = parse_context_entries(context_text)
	note(f"{len(audit_entries)} audited entry/entries, {len(threads_by_comment)} review comment id(s) keyed for thread lookup")

	plan: list[dict] = []
	planned_thread_id_indexes: dict[str, int] = {}
	for entry in sorted(audit_entries, key=lambda item: item["index"]):
		index = entry["index"]
		if entry["disposition"] == "applied" and not applied_changes_persisted:
			note(f"entry[{index}] claims an applied change without a pushed productive commit; leaving the thread open")
			continue
		if entry["disposition"] == "ignored" and not entry["reason"]:
			warn(f"entry[{index}] ignores a suggestion without a rationale; leaving the thread open")
			continue
		context = context_entries.get(index)
		if context is None:
			warn(f"entry[{index}] has no matching PR comment context entry; leaving any thread open")
			continue
		if context.get("kind") != "review_comment":
			note(f"entry[{index}] is a {context.get('kind') or 'unknown'} entry, which has no thread; skipping")
			continue
		if not paths_agree(entry["audit_path"], context.get("path", "")):
			warn(
				f"entry[{index}] audit path '{entry['audit_path']}' disagrees with comment path "
				f"'{context.get('path', '')}'; leaving the thread open"
			)
			continue
		raw_id = context.get("id", "")
		if not raw_id.isdigit():
			warn(f"entry[{index}] has a non-numeric comment id '{raw_id}'; leaving the thread open")
			continue
		thread = threads_by_comment.get(int(raw_id))
		if thread is None:
			note(f"entry[{index}] comment {raw_id} belongs to no open review thread; skipping")
			continue
		if thread.get("is_resolved"):
			note(f"entry[{index}] thread is already resolved; skipping")
			continue
		thread_id = thread.get("thread_id", "")
		if not isinstance(thread_id, str) or not thread_id:
			warn(f"entry[{index}] thread has no valid node id; leaving the thread open")
			continue
		top_level_reply_comment_id = thread.get("reply_comment_id")
		if "reply_comment_id" not in thread:
			thread_comment_ids = thread.get("comment_ids") or []
			top_level_reply_comment_id = thread_comment_ids[0] if thread_comment_ids else None
		if entry["disposition"] == "ignored" and (
			not isinstance(top_level_reply_comment_id, int) or top_level_reply_comment_id <= 0
		):
			warn(f"entry[{index}] thread has no top-level comment id for its rationale reply; leaving it open")
			continue
		plan_item = {
			"thread_id": thread_id,
			"comment_id": int(raw_id),
			"reply_comment_id": top_level_reply_comment_id,
			"disposition": entry["disposition"],
			"reason": entry["reason"],
			"path": context.get("path", "") or entry["audit_path"],
			"author": thread.get("author", ""),
			"resolution_reply_posted": bool(thread.get("resolution_reply_posted")),
		}
		planned_index = planned_thread_id_indexes.get(thread_id)
		if planned_index is not None:
			if entry["disposition"] == "ignored" and plan[planned_index].get("disposition") != "ignored":
				note(f"entry[{index}] thread {thread_id} replaces an earlier non-ignored plan row so its rationale is posted")
				plan[planned_index] = plan_item
			else:
				note(f"entry[{index}] thread {thread_id} is already planned from an earlier audited entry; skipping")
			continue
		planned_thread_id_indexes[thread_id] = len(plan)
		plan.append(plan_item)

	if len(plan) > max_threads:
		dropped = plan[max_threads:]
		warn(
			f"plan holds {len(plan)} thread(s), above REVIEW_RESOLVE_THREADS_MAX={max_threads}; "
			f"leaving {len(dropped)} open this run: "
			+ ", ".join(str(item["comment_id"]) for item in dropped)
		)
		plan = plan[:max_threads]

	try:
		with open(plan_file, "w", encoding="utf-8") as handle:
			for item in plan:
				handle.write(json.dumps(item, sort_keys=True) + "\n")
	except OSError as error:
		warn(f"could not write plan file ({error})")
		return 1

	note(f"plan holds {len(plan)} thread(s) to resolve")
	return 0


if __name__ == "__main__":
	sys.exit(build_plan())
