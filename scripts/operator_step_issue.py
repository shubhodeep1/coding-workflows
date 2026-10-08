#!/usr/bin/env python3
"""Keep the repository's `ai:operator-step` issue (docs/plans/replace-claude-sessions-with-cli-engine-plan.md, Q33).

Some gaps only a person can close: a missing secret or credential, a
repository variable to set, a release to tag, an operation the pipeline must
never perform by itself. The pipeline does not stop for them. Activation
verification (port P4) and the unblock judge's `operator_step` verdict record
each one here instead, and the work it belongs to stays safely off (a feature
flag that defaults off, or a placeholder env var named `*_UNSET_OPERATOR_STEP`)
until the operator acts.

There is one open `ai:operator-step` issue per repository. Its body starts
with `<!-- ai:operator-step:v1 -->`; each new entry is an immutable keyed
comment. For a key, the highest comment id is authoritative. Older comments
and legacy body sections remain visible as history, not overwritten by
concurrent writers.

Usage:

  operator_step_issue.py upsert --repo OWNER/REPO --key KEY --source TEXT --steps-file PATH

  operator_step_issue.py upsert ... [--source-sha SHA]

`--steps-file` holds a JSON array of `{"title": str, "instructions": str,
"dormant_until": str (optional)}`. `--key` is lower-case letters, digits and
`-` (for example `pr-123`, `project-45`, `unblock-77`). `--source-sha` (40
lower-case hex, optional) is the merge commit the steps came from; it is
written as the entry's second line, `<!-- ai:operator-step:source-sha=<sha> -->`.
Without it the output is unchanged and the entry is never ticked automatically.

Ticking entries once a release ships (plan item 4d of
docs/plans/unattended-claude-pipeline-completion-plan.md):

  operator_step_issue.py tick --repo OWNER/REPO --stable-sha SHA
      [--repo-dir DIR] [--trusted-login LOGIN]

The nightly promote cycle (scripts/promote_main_cycle.sh) runs it with the
commit the `stable` tag points at. For each key, the highest-id entry comment
written by the pipeline login is authoritative; comments by any other account
are ignored. An entry is ticked when that comment has a source-sha line, has
no `<!-- ai:operator-step:done stable=<sha> -->` line yet, and
`git -C DIR merge-base --is-ancestor <source> <stable>` succeeds. Ticking
appends one new comment for the key (every `- [ ]` becomes `- [x]`, plus the
done marker); older comments are never edited. A commit git cannot find is
not ticked, and `tick` never creates a tracker. Output is one JSON
line: `issue`, `stable`, `ticked` (keys), `skipped` (`{reason: count}`).
Each key logs `OPERATOR_STEP_TICK key= outcome=ticked|already_done|
no_source_sha|not_ancestor|unknown_commit stable=` on stderr. The
needs-human digest (below) is a different issue and is never touched.

Tick API budget (CLAUDE.md §15): at most one identity read (none with
`--trusted-login`), one tracker listing, one paginated comment read, and one
comment write per ticked key. No per-entry reads and no compare calls: the
ancestor check is local git.

Output is one JSON line: `issue`, `url`, `created`, `entries`. Exit 0 on
success, 1 on bad arguments, 2 when a GitHub call failed.

GitHub API budget (CLAUDE.md §15): one identity read, bounded tracker list
retries, a label lookup and issue create only when absent, and one paginated
comment read before an entry append. The issue listing has no comments, so
the paginated read cannot be folded into it. A stale post-create listing
retries rather than posting an entry to a duplicate tracker.

Needs-human digest (docs/plans/unattended-claude-pipeline-completion-plan.md,
item 4a, decision D6). When every automated judge round is spent, the item is
parked instead of closed: it keeps (or gets) `ai:needs-human` and gets one
line in the repository's single needs-human digest issue. That issue's body
starts with `<!-- ai:needs-human:v1 -->`. It carries the `ai:operator-step`
label, which keeps it out of clarify; it never carries `ai:needs-human`,
because the unblock scan and the staged-support latch sweep both key on that
label. Only a digest authored by the pipeline login is read or edited. The
body holds at most 200 entries; beyond that, item numbers go to an overflow
marker (at most 1,000, then only a dropped count). Parking never writes the
staged-support release marker, so `release_staged_support_needs_human_latches`
never auto-releases a parked item.

  operator_step_issue.py needs-human park --repo OWNER/REPO --item N
      --kind issue|pr|project --stop STOP --reason TEXT [--link URL]
      [--trusted-login LOGIN] [--item-labeled]
  operator_step_issue.py needs-human prune --repo OWNER/REPO [--trusted-login LOGIN]

`park` prints `issue`, `url`, `item`, `outcome` (parked|updated|skip),
`newly_parked`, `entries`, `overflow`. The caller sends the single CRITICAL
alert when `newly_parked` is true; this module never talks to Telegram.
`prune` drops entries whose item is no longer open with `ai:needs-human`
(merged, closed or cleared by a person) and promotes overflow items into
free slots; whenever it read the digest it prints `items`, the item numbers the
digest lists afterwards (entries and overflow; a dropped-count item is not
listed), which the unblock scan uses to tell a parked item from a stranded one. Kill switch `NEEDS_HUMAN_DIGEST_ENABLED` (default `true`):
`false` makes both subcommands a no-op with no API call. Each action logs
`NEEDS_HUMAN item= kind= reason= outcome=parked|updated|removed|skip` on stderr.

Digest API budget (CLAUDE.md §15). park: at most one identity read (none with
--trusted-login), one item label add (none with --item-labeled), one digest
listing, one label ensure and one create only when no digest exists, at most
one PATCH per attempt and at most MAX_UPSERT_ATTEMPTS verification listings;
no per-item reads. After a create, one more listing: when an older digest is
found (two writers created one at once), the new issue is closed (one PATCH)
and the entry goes to the older digest. A failed digest write never removes
`ai:needs-human` (the label may be a human latch that predates the call); the
unblock scan treats an item as parked only while the digest lists it (prune
prints `items`), so an item left labelled without a digest line is judged and
parked again. prune: one digest listing and, only when the digest has
entries, the open `ai:needs-human` listing (one call per 100 items) plus,
only when the body changes or a duplicate digest exists, one re-listing (a
concurrent writer to the digest or a duplicate since the first read skips the
tick) and at most one PATCH. Newer duplicate digests
(left open when a park's post-create check failed) are merged into the oldest
one and closed, one PATCH each. The body PATCH is not
atomic; the re-listing narrows the window but does not close it. The unblock scan's search (30 items, lagging index) and the
staged-support sweep's listing (source repo only, PRs dropped) were audited
and cannot answer the prune question.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LABEL = "ai:operator-step"
MARKER = "<!-- ai:operator-step:v1 -->"
TITLE = "Operator steps waiting"
ENTRY_RE = re.compile(r"^<!-- ai:operator-step:entry key=([a-z0-9][a-z0-9-]{0,63}) -->$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
MAX_BODY = 60000
MAX_STEPS = 20
MAX_FIELD = 2000
MAX_UPSERT_ATTEMPTS = 3
SOURCE_SHA_RE = re.compile(r"^<!-- ai:operator-step:source-sha=([0-9a-f]{40}) -->$")
DONE_RE = re.compile(r"^<!-- ai:operator-step:done stable=([0-9a-f]{40}) -->$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_ANCESTOR_TIMEOUT_SECS = 60
INTRO = (
	"The pipeline found steps only a person can take. The work they belong to keeps running "
	"where it can and stays off where it must until each step is done. Tick a step when you "
	"have done it; close this issue when nothing is left."
)


class UsageError(Exception):
	"""Bad arguments: exit 1."""


class ApiError(Exception):
	"""A GitHub call failed: exit 2."""


def _clean(value: object, limit: int = MAX_FIELD) -> str:
	if value is None:
		return ""
	if not isinstance(value, str):
		raise UsageError("step fields must be strings")
	text = value.replace("<!--", "").replace("-->", "").replace("\r", "")
	return text.strip()[:limit]


def _gh(args: list[str], *, allow_existing_label: bool = False) -> str:
	try:
		result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
	except OSError as exc:
		raise ApiError(f"gh {args[0] if args else ''} failed: {exc}") from exc
	if result.returncode != 0:
		if allow_existing_label and re.search(r"\blabel\b[^\r\n]*\balready[ _-]*exists\b|\balready_exists\b", result.stderr, re.IGNORECASE):
			return ""
		raise ApiError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
	return result.stdout


def _ensure_operator_label(repo: str) -> None:
	# Read metadata from the same trusted support checkout as this writer, not the target repo.
	contract_path = Path(__file__).resolve().parent.parent / ".github/ai/label_contract.v1.json"
	try:
		registration = json.loads(contract_path.read_text(encoding="utf-8"))["labels"][LABEL]
	except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
		raise ApiError("operator-step label registration unavailable") from exc
	if not isinstance(registration, dict):
		raise ApiError("invalid operator-step label registration")
	color = registration.get("color")
	description = registration.get("description")
	if (not isinstance(color, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", color)
		or not isinstance(description, str) or not description or len(description) > 100
		or not description.isprintable()):
		raise ApiError("invalid operator-step label registration")
	_gh(["label", "create", LABEL, "--repo", repo, "--color", color,
		"--description", description], allow_existing_label=True)


def _trusted(issue: dict) -> bool:
	user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
	login = str(user.get("login") or "")
	return login == "github-actions[bot]" or issue.get("author_association") in TRUSTED_ASSOCIATIONS


def find_issue(issues: object) -> dict | None:
	"""The oldest open, trusted issue whose body starts with the marker."""
	candidates = find_issues(issues)
	return candidates[0] if candidates else None


def find_issues(issues: object) -> list[dict]:
	"""All open, trusted tracker issues in issue-number order."""
	if not isinstance(issues, list):
		return []
	candidates = [
		issue
		for issue in issues
		if isinstance(issue, dict)
		and "pull_request" not in issue
		and _trusted(issue)
		and str(issue.get("body") or "").split("\n", 1)[0].strip() == MARKER
	]
	return sorted(candidates, key=lambda issue: int(issue.get("number") or 0))


def parse_entries(body: str) -> list[tuple[str, str]]:
	"""(key, section text) pairs in body order; text before the first entry is ignored."""
	entries: list[tuple[str, list[str]]] = []
	for line in body.replace("\r\n", "\n").split("\n"):
		match = ENTRY_RE.match(line.strip())
		if match:
			entries.append((match.group(1), [line.strip()]))
		elif entries:
			entries[-1][1].append(line)
	return [(key, "\n".join(lines).rstrip()) for key, lines in entries]


def render_entry(key: str, source: str, steps: list[dict], source_sha: str | None = None) -> str:
	lines = [f"<!-- ai:operator-step:entry key={key} -->"]
	if source_sha:
		lines.append(f"<!-- ai:operator-step:source-sha={source_sha} -->")
	lines += [f"### {_clean(source, 300) or key}", ""]
	for step in steps:
		lines.append(f"- [ ] **{_clean(step.get('title'), 200) or 'Operator step'}**")
		instructions = _clean(step.get("instructions"))
		for instruction_line in instructions.split("\n"):
			if instruction_line.strip():
				lines.append(f"  {instruction_line.rstrip()}")
		dormant = _clean(step.get("dormant_until"), 300)
		if dormant:
			lines.append(f"  Stays off until then: `{dormant}`.")
	return "\n".join(lines).rstrip()


def render_body(entries: list[tuple[str, str]]) -> str:
	parts = [MARKER, "## Operator steps", INTRO]
	kept = list(entries)
	while True:
		body = "\n\n".join(parts + [text for _, text in kept]) + "\n"
		if len(body.encode("utf-8")) <= MAX_BODY:
			return body
		if len(kept) <= 1:
			raise UsageError("operator-step entry exceeds the issue body limit")
		kept.pop(0)


def load_entry_comments(repo: str, issue_number: int) -> list[dict]:
	"""Read all comment pages before deduplicating an operator-step key."""
	raw = _gh(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{issue_number}/comments?per_page=100"])
	try:
		pages = json.loads(raw)
	except ValueError as exc:
		raise ApiError(f"unreadable operator-step comments: {exc}") from exc
	if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
		raise ApiError("unreadable operator-step comments: expected pages of comments")
	comments = [comment for page in pages for comment in page]
	if any(not isinstance(comment, dict) for comment in comments):
		raise ApiError("unreadable operator-step comments: malformed comment")
	return comments


def load_steps(path: str) -> list[dict]:
	try:
		steps = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, UnicodeDecodeError, ValueError) as exc:
		raise UsageError(f"cannot read --steps-file {path}: {exc}") from exc
	if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) for step in steps):
		raise UsageError("--steps-file must hold a non-empty JSON array of objects")
	if len(steps) > MAX_STEPS:
		raise UsageError(f"--steps-file exceeds {MAX_STEPS} operator steps")
	return steps


def upsert(repo: str, key: str, source: str, steps: list[dict], source_sha: str | None = None) -> dict:
	if not REPO_RE.match(repo):
		raise UsageError(f"--repo must be OWNER/REPO, got {repo!r}")
	if not KEY_RE.match(key):
		raise UsageError(f"--key must be lower-case letters, digits and '-', got {key!r}")
	if source_sha and not SHA_RE.match(source_sha):
		raise UsageError(f"--source-sha must be 40 lower-case hex characters, got {source_sha!r}")
	entry = render_entry(key, source, steps, source_sha or None)
	# The tracker listing has no comment authors. Resolve the credential's
	# identity once to avoid treating an unrelated user's marker as ours.
	trusted_login = _gh(["api", "user", "--jq", ".login"]).strip()
	if not trusted_login:
		raise ApiError("operator-step writer identity is unavailable")
	created_number: int | None = None
	for _upsert_attempt in range(MAX_UPSERT_ATTEMPTS + 1):
		listing = _gh(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=30"])
		try:
			issues = json.loads(listing or "[]")
		except ValueError as exc:
			raise ApiError(f"unreadable issue list: {exc}") from exc
		candidates = find_issues(issues)
		# A direct read of a newly created tracker cannot prove that an older
		# concurrent tracker is absent. Wait for the canonical label listing.
		if not candidates:
			if created_number is not None:
				if _upsert_attempt < MAX_UPSERT_ATTEMPTS:
					time.sleep(2 ** _upsert_attempt)
					continue
				raise ApiError("operator-step tracker listing did not converge after creation")
			_ensure_operator_label(repo)
			try:
				created = json.loads(_gh(["api", f"repos/{repo}/issues", "-f", f"title={TITLE}", "-f", f"body={render_body([])}", "-f", f"labels[]={LABEL}"]) or "{}")
			except ValueError as exc:
				raise ApiError(f"unreadable created issue: {exc}") from exc
			created_number = int(created.get("number") or 0) or None
			if created_number is None:
				raise ApiError("operator-step issue creation returned no issue number")
			continue

		existing = candidates[0]
		number = int(existing["number"])
		comments = load_entry_comments(repo, number)
		marker = f"<!-- ai:operator-step:entry key={key} -->"
		matches = sorted(
			(
				comment for comment in comments
				if (comment.get("user") or {}).get("login") == trusted_login
				and str(comment.get("body") or "").split("\n", 1)[0].strip() == marker
				and isinstance(comment.get("id"), int)
			),
			key=lambda comment: comment["id"],
		)
		legacy = dict(parse_entries(str(existing.get("body") or "")))
		comment_keys = {
			entry_match.group(1)
			for comment in comments if (comment.get("user") or {}).get("login") == trusted_login
			for entry_match in [ENTRY_RE.match(str(comment.get("body") or "").split("\n", 1)[0].strip())]
			if entry_match
		}
		if (matches[-1]["body"] if matches else legacy.get(key, "")).replace("\r\n", "\n").rstrip() != entry:
			_gh(["api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={entry}"])
		# Never patch the oldest comment with a proposal from a stale listing:
		# a later writer's higher-id comment would otherwise be lost.
		return {
			"issue": number,
			"url": existing.get("html_url", ""),
			"created": created_number == number,
			"entries": len(set(legacy) | comment_keys | {key}),
		}
	raise ApiError(f"operator-step tracker did not converge after {MAX_UPSERT_ATTEMPTS} attempts")


# --- ticking entries after a release (plan item 4d) ----------------------------


def _authoritative_entry_comments(comments: list[dict], trusted_login: str) -> dict[str, dict]:
	"""The highest-id entry comment per key, written by the pipeline login only."""
	latest: dict[str, dict] = {}
	for comment in comments:
		user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
		if not trusted_login or user.get("login") != trusted_login or not isinstance(comment.get("id"), int):
			continue
		first = str(comment.get("body") or "").replace("\r\n", "\n").split("\n", 1)[0].strip()
		match = ENTRY_RE.match(first)
		if not match:
			continue
		key = match.group(1)
		if key not in latest or comment["id"] > latest[key]["id"]:
			latest[key] = comment
	return latest


def _is_ancestor(repo_dir: str, source_sha: str, stable_sha: str) -> str:
	"""ancestor | not_ancestor | unknown_commit (git error, missing commit, timeout)."""
	try:
		result = subprocess.run(
			["git", "-C", repo_dir, "merge-base", "--is-ancestor", source_sha, stable_sha],
			capture_output=True, text=True, check=False, timeout=GIT_ANCESTOR_TIMEOUT_SECS,
		)
	except (OSError, subprocess.TimeoutExpired):
		return "unknown_commit"
	if result.returncode == 0:
		return "ancestor"
	if result.returncode == 1:
		return "not_ancestor"
	return "unknown_commit"


def _render_ticked(body: str, stable_sha: str, source_sha: str) -> str:
	lines = [
		"- [x]" + line[len("- [ ]"):] if line.startswith("- [ ]") else line
		for line in body.replace("\r\n", "\n").rstrip().split("\n")
	]
	lines += [
		"",
		f"<!-- ai:operator-step:done stable={stable_sha} -->",
		f"Done: the `stable` tag (`{stable_sha[:7]}`) includes the merge `{source_sha[:7]}`; ticked by the promote cycle.",
	]
	return "\n".join(lines)


def tick(repo: str, stable_sha: str, repo_dir: str = ".", trusted_login: str | None = None) -> dict:
	if not REPO_RE.match(repo):
		raise UsageError(f"--repo must be OWNER/REPO, got {repo!r}")
	if not isinstance(stable_sha, str) or not SHA_RE.match(stable_sha):
		raise UsageError(f"--stable-sha must be 40 lower-case hex characters, got {stable_sha!r}")
	login = (trusted_login or "").strip() or _gh(["api", "user", "--jq", ".login"]).strip()
	if not login:
		raise ApiError("operator-step tick identity is unavailable")
	result: dict = {"issue": None, "stable": stable_sha, "ticked": [], "skipped": {}}
	listing = _gh(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=30"])
	try:
		issues = json.loads(listing or "[]")
	except ValueError as exc:
		raise ApiError(f"unreadable issue list: {exc}") from exc
	candidates = find_issues(issues)
	if not candidates:
		return result
	number = int(candidates[0]["number"])
	result["issue"] = number
	latest = _authoritative_entry_comments(load_entry_comments(repo, number), login)
	for key in sorted(latest):
		body = str(latest[key].get("body") or "").replace("\r\n", "\n")
		lines = [line.strip() for line in body.split("\n")]
		source_match = SOURCE_SHA_RE.match(lines[1]) if len(lines) > 1 else None
		if any(DONE_RE.match(line) for line in lines):
			outcome = "already_done"
		elif not source_match:
			outcome = "no_source_sha"
		else:
			ancestry = _is_ancestor(repo_dir, source_match.group(1), stable_sha)
			outcome = "ticked" if ancestry == "ancestor" else ancestry
		if outcome == "ticked":
			_gh(["api", f"repos/{repo}/issues/{number}/comments", "-f",
				f"body={_render_ticked(body, stable_sha, source_match.group(1))}"])
			result["ticked"].append(key)
		else:
			result["skipped"][outcome] = result["skipped"].get(outcome, 0) + 1
		print(f"OPERATOR_STEP_TICK key={key} outcome={outcome} stable={stable_sha[:7]}", file=sys.stderr)
	return result


# --- needs-human digest (plan item 4a, D6) ------------------------------------

NEEDS_HUMAN_MARKER = "<!-- ai:needs-human:v1 -->"
NEEDS_HUMAN_LABEL = "ai:needs-human"
NEEDS_HUMAN_TITLE = "Items waiting for a person"
NEEDS_HUMAN_MAX_ENTRIES = 200
NEEDS_HUMAN_OVERFLOW_MAX = 1000
NEEDS_HUMAN_KINDS = ("issue", "pr", "project")
NEEDS_HUMAN_ENTRY_RE = re.compile(
	r"<!-- ai:needs-human:entry item=([1-9][0-9]{0,9}) kind=(issue|pr|project) "
	r"parked=([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z) -->$"
)
NEEDS_HUMAN_OVERFLOW_RE = re.compile(r"^<!-- ai:needs-human:overflow items=([0-9,]*) dropped=([0-9]+) -->$")
NEEDS_HUMAN_STOP_RE = re.compile(r"^[a-z][a-z-]{0,63}$")
NEEDS_HUMAN_INTRO = (
	"Every automated judge round is spent for the items below. They stay open and wait "
	"for a person to decide. An entry disappears once its item is merged or closed, or "
	"once `ai:needs-human` is removed from it."
)


def needs_human_enabled() -> bool:
	return os.environ.get("NEEDS_HUMAN_DIGEST_ENABLED", "true").strip().lower() != "false"


def _needs_human_log(item: object, kind: str, reason: str, outcome: str) -> None:
	safe_reason = re.sub(r"[^A-Za-z0-9_.:-]", "_", reason)[:80] or "none"
	print(f"NEEDS_HUMAN item={item} kind={kind} reason={safe_reason} outcome={outcome}", file=sys.stderr)


def _one_line(value: object, limit: int) -> str:
	return " ".join(_clean(value, MAX_FIELD).split())[:limit]


def _digest_trusted(issue: dict, trusted_login: str) -> bool:
	"""Stricter than _trusted: only the pipeline login may author the digest."""
	user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
	return bool(trusted_login) and user.get("login") == trusted_login


def find_digest(issues: object, trusted_login: str) -> dict | None:
	"""The oldest open, pipeline-authored issue whose body starts with the digest marker."""
	candidates = find_digests(issues, trusted_login)
	return candidates[0] if candidates else None


def find_digests(issues: object, trusted_login: str) -> list[dict]:
	"""All open, pipeline-authored digest issues, oldest first (later ones are duplicates)."""
	if not isinstance(issues, list):
		return []
	candidates = [
		issue
		for issue in issues
		if isinstance(issue, dict)
		and "pull_request" not in issue
		and _digest_trusted(issue, trusted_login)
		and str(issue.get("body") or "").split("\n", 1)[0].strip() == NEEDS_HUMAN_MARKER
	]
	candidates.sort(key=lambda issue: int(issue.get("number") or 0))
	return candidates


def parse_digest(body: str) -> tuple[dict[int, dict], list[int], int]:
	"""(entries keyed by item, overflow item numbers, dropped count)."""
	entries: dict[int, dict] = {}
	overflow: list[int] = []
	dropped = 0
	for raw in str(body or "").replace("\r\n", "\n").split("\n"):
		line = raw.strip()
		match = NEEDS_HUMAN_ENTRY_RE.search(line)
		if match and line.startswith("- #"):
			item = int(match.group(1))
			entries.setdefault(item, {"kind": match.group(2), "parked": match.group(3), "line": line})
			continue
		match = NEEDS_HUMAN_OVERFLOW_RE.match(line)
		if match:
			overflow = [int(n) for n in match.group(1).split(",") if n.isdigit() and int(n) > 0]
			dropped = int(match.group(2))
	overflow = [n for n in dict.fromkeys(overflow) if n not in entries]
	return entries, overflow, dropped


def render_digest_entry(item: int, kind: str, stop: str, reason: str, link: str, parked: str) -> str:
	parts = [f"- #{item} ({kind})", f"stop `{stop}`" if stop else ""]
	text = _one_line(reason, 160)
	if text:
		parts.append(text)
	if link:
		# A poller-parked project links its tracking issue, not a verdict comment.
		parts.append(f"[{'last verdict' if '#issuecomment-' in link else 'details'}]({link})")
	parts.append(f"parked {parked}")
	return "; ".join(part for part in parts if part) + f" <!-- ai:needs-human:entry item={item} kind={kind} parked={parked} -->"


def render_digest(entries: dict[int, dict], overflow: list[int], dropped: int = 0) -> str:
	ordered = sorted(entries.items(), key=lambda pair: (pair[1]["parked"], pair[0]))
	kept = ordered[:NEEDS_HUMAN_MAX_ENTRIES]
	spill = [item for item, _ in ordered[NEEDS_HUMAN_MAX_ENTRIES:]]
	while True:
		extra = list(dict.fromkeys(spill + overflow))
		lost = max(0, len(extra) - NEEDS_HUMAN_OVERFLOW_MAX)
		extra = extra[:NEEDS_HUMAN_OVERFLOW_MAX]
		parts = [NEEDS_HUMAN_MARKER, f"## {NEEDS_HUMAN_TITLE}", NEEDS_HUMAN_INTRO]
		lines = [entry["line"] for _, entry in kept]
		parts.append("\n".join(lines) if lines else "_Nothing is waiting._")
		total_dropped = dropped + lost
		if extra or total_dropped:
			parts.append(
				f"<!-- ai:needs-human:overflow items={','.join(str(n) for n in extra)} dropped={total_dropped} -->\n"
				f"...and {len(extra) + total_dropped} more parked items carry `{NEEDS_HUMAN_LABEL}`."
			)
		body = "\n\n".join(parts) + "\n"
		if len(body.encode("utf-8")) <= MAX_BODY or not kept:
			return body
		# Keep the body under the issue limit: the newest entry moves to overflow.
		spill.insert(0, kept.pop()[0])


def _list_digests(repo: str, trusted_login: str) -> list[dict]:
	listing = _gh(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=30"])
	try:
		issues = json.loads(listing or "[]")
	except ValueError as exc:
		raise ApiError(f"unreadable issue list: {exc}") from exc
	return find_digests(issues, trusted_login)


def _list_digest(repo: str, trusted_login: str) -> dict | None:
	digests = _list_digests(repo, trusted_login)
	return digests[0] if digests else None


def _resolve_login(trusted_login: str | None) -> str:
	login = (trusted_login or "").strip() or _gh(["api", "user", "--jq", ".login"]).strip()
	if not login:
		raise ApiError("needs-human digest writer identity is unavailable")
	return login


def _now_iso() -> str:
	return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def needs_human_park(
	repo: str,
	item: int,
	kind: str,
	stop: str,
	reason: str,
	link: str = "",
	trusted_login: str | None = None,
	item_labeled: bool = False,
	now: str | None = None,
) -> dict:
	if not REPO_RE.match(repo):
		raise UsageError(f"--repo must be OWNER/REPO, got {repo!r}")
	if not isinstance(item, int) or item < 1:
		raise UsageError("--item must be a positive issue or PR number")
	if kind not in NEEDS_HUMAN_KINDS:
		raise UsageError(f"--kind must be one of {list(NEEDS_HUMAN_KINDS)}")
	if not NEEDS_HUMAN_STOP_RE.match(stop or ""):
		raise UsageError(f"--stop must be a lower-case stop id, got {stop!r}")
	link_re = re.compile(rf"^https://github\.com/{re.escape(repo)}/(issues|pull)/[0-9]+(#issuecomment-[0-9]+)?$")
	link = link.strip() if isinstance(link, str) and link_re.match(link.strip()) else ""
	reason_text = _one_line(reason, 160)
	log_reason = reason_text.split(" ", 1)[0] if reason_text else stop
	if not needs_human_enabled():
		_needs_human_log(item, kind, log_reason, "skip")
		return {"issue": None, "url": "", "item": item, "outcome": "skip", "reason": "disabled", "newly_parked": False}
	login = _resolve_login(trusted_login)
	if not item_labeled:
		# Label first: prune keys on the label, so an entry is never listed without it.
		# The label is never rolled back on a failed digest write: it may be a
		# pre-existing human latch, and the scan re-judges an item the digest
		# does not list (see needs_human_prune `items`).
		_gh(["api", "-X", "POST", f"repos/{repo}/issues/{item}/labels", "-f", f"labels[]={NEEDS_HUMAN_LABEL}"])
	try:
		return _needs_human_park_digest(repo, item, kind, stop, reason_text, link, login, log_reason, now)
	except (KeyError, ValueError, TypeError) as exc:
		raise ApiError(f"needs-human digest write failed: {exc}") from exc


def _needs_human_park_digest(repo: str, item: int, kind: str, stop: str, reason_text: str, link: str,
		login: str, log_reason: str, now: str | None) -> dict:
	stamp = now or _now_iso()
	newly_parked: bool | None = None
	wrote = False
	created_number = 0
	for _attempt in range(MAX_UPSERT_ATTEMPTS + 1):
		digest = _list_digest(repo, login)
		if digest is None:
			if wrote or created_number:
				# The digest left the open listing after our write; never create a
				# second one and never report a park no open digest records.
				raise ApiError("needs-human digest disappeared from the open listing after a write")
			_ensure_operator_label(repo)
			entries = {item: {"kind": kind, "parked": stamp, "line": render_digest_entry(item, kind, stop, reason_text, link, stamp)}}
			try:
				created = json.loads(_gh(["api", f"repos/{repo}/issues", "-f", f"title={NEEDS_HUMAN_TITLE}",
					"-f", f"body={render_digest(entries, [])}", "-f", f"labels[]={LABEL}"]) or "{}")
			except ValueError as exc:
				raise ApiError(f"unreadable created digest: {exc}") from exc
			number = int(created.get("number") or 0)
			if not number:
				raise ApiError("needs-human digest creation returned no issue number")
			try:
				older = _list_digest(repo, login)
			except ApiError:
				# The digest with this entry exists: report the park so the caller
				# still sends its one CRITICAL alert. A duplicate digest left open
				# here is merged into the oldest one and closed by the next prune.
				print(f"::warning::needs-human digest #{number}: duplicate check failed; "
					"the next prune merges any duplicate digest", file=sys.stderr)
				older = None
			if older is not None and int(older.get("number") or 0) < number:
				# A concurrent writer created a digest first: keep the oldest one only.
				_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=closed", "-f", "state_reason=not_planned"])
				created_number = number
				continue
			_needs_human_log(item, kind, log_reason, "parked")
			return {"issue": number, "url": created.get("html_url", ""), "item": item, "outcome": "parked",
				"newly_parked": True, "entries": 1, "overflow": 0}
		body = str(digest.get("body") or "")
		entries, overflow, dropped = parse_digest(body)
		if newly_parked is None:
			newly_parked = item not in entries and item not in overflow
		if item in entries:
			parked = entries[item]["parked"]
			entries[item] = {"kind": kind, "parked": parked, "line": render_digest_entry(item, kind, stop, reason_text, link, parked)}
		elif item not in overflow:
			if len(entries) < NEEDS_HUMAN_MAX_ENTRIES:
				entries[item] = {"kind": kind, "parked": stamp, "line": render_digest_entry(item, kind, stop, reason_text, link, stamp)}
			elif len(overflow) < NEEDS_HUMAN_OVERFLOW_MAX:
				overflow.append(item)
			elif not wrote:
				dropped += 1
		new_body = render_digest(entries, overflow, dropped)
		number = int(digest["number"])
		if new_body.replace("\r\n", "\n") == body.replace("\r\n", "\n"):
			break
		_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", f"body={new_body}"])
		wrote = True
	else:
		raise ApiError(f"needs-human digest did not converge after {MAX_UPSERT_ATTEMPTS} attempts")
	outcome = "parked" if newly_parked else "updated"
	_needs_human_log(item, kind, log_reason, outcome)
	return {"issue": int(digest["number"]) if digest else None, "url": (digest or {}).get("html_url", ""), "item": item,
		"outcome": outcome, "newly_parked": bool(newly_parked), "entries": len(entries), "overflow": len(overflow)}


def needs_human_prune(repo: str, trusted_login: str | None = None, now: str | None = None) -> dict:
	if not REPO_RE.match(repo):
		raise UsageError(f"--repo must be OWNER/REPO, got {repo!r}")
	if not needs_human_enabled():
		_needs_human_log("none", "digest", "disabled", "skip")
		return {"issue": None, "outcome": "skip", "reason": "disabled", "removed": [], "items": []}
	login = _resolve_login(trusted_login)
	digests = _list_digests(repo, login)
	if not digests:
		return {"issue": None, "outcome": "skip", "reason": "no_digest", "removed": [], "items": []}
	digest, duplicates = digests[0], digests[1:]
	body = str(digest.get("body") or "")
	entries, overflow, dropped = parse_digest(body)
	# Two parks can create a digest each when the post-create duplicate check
	# fails; fold every newer duplicate into the oldest digest, then close it.
	for duplicate in duplicates:
		dup_entries, dup_overflow, dup_dropped = parse_digest(str(duplicate.get("body") or ""))
		for number, entry in dup_entries.items():
			if number not in overflow:
				entries.setdefault(number, entry)
		overflow.extend(n for n in dup_overflow if n not in entries and n not in overflow)
		dropped += dup_dropped
	if not entries and not overflow and not duplicates:
		return {"issue": int(digest["number"]), "outcome": "skip", "reason": "empty", "removed": [], "items": []}
	raw = _gh(["api", "--paginate", "--slurp", f"repos/{repo}/issues?state=open&labels={NEEDS_HUMAN_LABEL}&per_page=100"])
	try:
		pages = json.loads(raw)
	except ValueError as exc:
		raise ApiError(f"unreadable needs-human listing: {exc}") from exc
	if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
		raise ApiError("unreadable needs-human listing: expected pages of issues")
	open_items: dict[int, str] = {}
	for issue in (issue for page in pages for issue in page):
		if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
			raise ApiError("unreadable needs-human listing: malformed issue")
		labels = {label.get("name") if isinstance(label, dict) else label for label in issue.get("labels") or []}
		open_items[issue["number"]] = "pr" if "pull_request" in issue else ("project" if "ai:orchestrator-tracking" in labels else "issue")
	removed: list[int] = []
	for number in list(entries):
		if number not in open_items:
			removed.append(number)
			_needs_human_log(number, entries.pop(number)["kind"], "closed_or_cleared", "removed")
	for number in list(overflow):
		if number not in open_items:
			overflow.remove(number)
			removed.append(number)
			# Overflow keeps only item numbers, so the kind of a removed item is unknown.
			_needs_human_log(number, "unknown", "closed_or_cleared", "removed")
	stamp = now or _now_iso()
	while overflow and len(entries) < NEEDS_HUMAN_MAX_ENTRIES:
		number = overflow.pop(0)
		kind = open_items[number]
		entries[number] = {"kind": kind, "parked": stamp, "line": render_digest_entry(number, kind, "", "(details in item)", "", stamp)}
	new_body = render_digest(entries, overflow, dropped)
	body_changed = new_body.replace("\r\n", "\n") != body.replace("\r\n", "\n")
	if body_changed or duplicates:
		# Narrow the read-modify-write window: if a concurrent park changed the
		# digest, or a duplicate about to be closed, since our read, skip this
		# tick instead of overwriting or closing away its entry.
		current_all = {int(d.get("number") or 0): str(d.get("body") or "") for d in _list_digests(repo, login)}
		current_body = current_all.get(int(digest["number"]))
		current = None if current_body is None else {"body": current_body}
		dup_changed = any(current_all.get(int(dup["number"]), str(dup.get("body") or "")) != str(dup.get("body") or "")
			for dup in duplicates)
		if current is None or current_body != body or dup_changed:
			_needs_human_log("none", "digest", "concurrent_update", "skip")
			current_entries, current_overflow, _ = parse_digest(str((current or {}).get("body") or ""))
			dup_items = {n for dup in duplicates
				for part in parse_digest(current_all.get(int(dup["number"]), str(dup.get("body") or "")))[:2] for n in part}
			return {"issue": int(digest["number"]), "outcome": "skip", "reason": "concurrent_update", "removed": [],
				"items": sorted(set(current_entries) | set(current_overflow) | dup_items)}
		if body_changed:
			_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{int(digest['number'])}", "-f", f"body={new_body}"])
	for duplicate in duplicates:
		# Only after the oldest digest holds the merged entries; a failed close
		# is retried by the next prune (the merge is idempotent).
		try:
			_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{int(duplicate['number'])}", "-f", "state=closed",
				"-f", "state_reason=not_planned"])
			_needs_human_log("none", "digest", f"duplicate_{int(duplicate['number'])}_merged", "removed")
		except ApiError:
			_needs_human_log("none", "digest", f"duplicate_{int(duplicate['number'])}_close_failed", "skip")
	return {"issue": int(digest["number"]), "outcome": "pruned" if removed else "unchanged", "removed": removed,
		"entries": len(entries), "overflow": len(overflow), "items": sorted(set(entries) | set(overflow))}


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	upsert_cmd = sub.add_parser("upsert")
	upsert_cmd.add_argument("--repo", required=True)
	upsert_cmd.add_argument("--key", required=True)
	upsert_cmd.add_argument("--source", required=True)
	upsert_cmd.add_argument("--steps-file", required=True)
	upsert_cmd.add_argument("--source-sha", default="")
	tick_cmd = sub.add_parser("tick")
	tick_cmd.add_argument("--repo", required=True)
	tick_cmd.add_argument("--stable-sha", required=True)
	tick_cmd.add_argument("--repo-dir", default=".")
	tick_cmd.add_argument("--trusted-login", default="")
	digest_cmd = sub.add_parser("needs-human")
	digest_sub = digest_cmd.add_subparsers(dest="digest_command", required=True)
	park_cmd = digest_sub.add_parser("park")
	park_cmd.add_argument("--repo", required=True)
	park_cmd.add_argument("--item", required=True, type=int)
	park_cmd.add_argument("--kind", required=True)
	park_cmd.add_argument("--stop", required=True)
	park_cmd.add_argument("--reason", default="")
	park_cmd.add_argument("--link", default="")
	park_cmd.add_argument("--trusted-login", default="")
	park_cmd.add_argument("--item-labeled", action="store_true")
	prune_cmd = digest_sub.add_parser("prune")
	prune_cmd.add_argument("--repo", required=True)
	prune_cmd.add_argument("--trusted-login", default="")
	args = parser.parse_args(argv)
	try:
		if args.command == "needs-human" and args.digest_command == "park":
			result = needs_human_park(args.repo, args.item, args.kind, args.stop, args.reason, args.link,
				args.trusted_login or None, args.item_labeled)
		elif args.command == "needs-human":
			result = needs_human_prune(args.repo, args.trusted_login or None)
		elif args.command == "tick":
			result = tick(args.repo, args.stable_sha, args.repo_dir, args.trusted_login or None)
		else:
			result = upsert(args.repo, args.key, args.source, load_steps(args.steps_file), args.source_sha or None)
	except UsageError as exc:
		print(json.dumps({"error": str(exc)}))
		return 1
	except ApiError as exc:
		print(json.dumps({"error": str(exc)}))
		return 2
	print(json.dumps(result))
	return 0


if __name__ == "__main__":
	sys.exit(main())
