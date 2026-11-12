#!/usr/bin/env python3
"""Keep the repository's `ai:operator-step` issue (docs/plans/replace-claude-sessions-with-cli-engine-plan.md, Q33).

Some gaps only a person can close: a missing secret or credential, a
repository variable to set, a release to tag, an operation the pipeline must
never perform by itself. The pipeline does not stop for them. Activation
verification (port P4) and the unblock judge's `operator_step` verdict record
each one here instead, and the new work it belongs to stays safely off (a
feature flag that defaults off, or a placeholder env var named
`*_UNSET_OPERATOR_STEP`) until the operator acts. The flag gates only the new
code path; everything that already runs keeps running. The judge also lists a
blocked fix-up of its own here (key `unblock-fixup-<n>`) instead of filing a
fix-up for it.

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

Resolving released entries (stuck-states P10, Refs #8291):

  operator_step_issue.py resolve --repo OWNER/REPO [--trusted-login LOGIN]

The poller (`.github/workflows/orchestrate_poll.yml`, step "Resolve released
operator steps") runs it on every tick. It ticks an entry only when its key is
`pr-<n>` or `project-<n>`, its authoritative comment (highest id by the
pipeline login) has a source-sha line and no done/resolved marker, and every
unticked step is a release step: its `Stays off until then:` value starts
with `@stable contains` (RELEASE_CONDITION_RE, kept in sync with
scripts/activation_verify.sh), or it has no such line and its title is a
release verb naming `stable` or the consumer sync. A step whose own dormant
line names anything else is a human step whatever its title says, a
title-only step whose title names a secret, token, credential, password,
variable, setting, permission, account or key is a human step too, and an
entry that also holds a real human step (a secret, a setting) is never ticked. Containment is `GET compare/<source>...stable`:
only `ahead` or `identical` count; `behind`, `diverged` or an unreadable
status do not. Compares are cached per commit and capped at
MAX_RESOLVE_COMPARES per run, oldest entry first; the first compare error
(for example a 404 in a repository with no `stable` tag) halts the rest of
the run's compares. Ticking appends one superseding comment (every `- [ ]`
becomes `- [x]`, plus `<!-- ai:operator-step:resolved release=stable
status=ahead|identical -->`); `tick` treats that marker as done.
When no unticked step is left on any entry (and at least one entry exists),
the tracker was not reopened by a person (`state_reason` `reopened`), and a
re-read of the comments shows no change, it posts one
`<!-- ai:operator-step:auto-closed -->` comment (skipped when a trusted one
newer than every entry already exists, i.e. an earlier close PATCH failed)
and closes the tracker as `completed`. One more comment read after the
close reopens the tracker (`state=open`, reason `reopened_after_close`) when
an entry was written in between; `upsert` re-runs its open-tracker listing
after posting and reopens a tracker that is no longer listed, so an entry
never sits on a closed tracker. Both checks fail safe: a failed read after
the close reopens the tracker before exiting 2 (reason
`after_close_read_failed`), and a failed `upsert` re-listing or reopen exits 3
(entry posted, tracker state unknown). This
narrows the close race; it does not make it atomic. A tracker the pipeline reopened is then
treated like one a person reopened and is never closed again. Kill switch
`OPERATOR_STEP_AUTO_RESOLVE_ENABLED` (default `true`): `false` makes it a
no-op with no API call. Output is one JSON line: `issue`, `ticked`,
`skipped` (`{reason: count}`), `compares`, `unticked`, `closed`. Each key logs
`OPERATOR_STEP_RESOLVE key= outcome=ticked|already_done|not_release|
no_source_sha|deferred|not_contained|compare_failed status=` and the close
decision logs `OPERATOR_STEP_RESOLVE op=close issue= outcome=closed|open
reason=` on stderr. The needs-human digest is never touched.

Resolve API budget (CLAUDE.md §14): one tracker listing (and nothing else
when no tracker is open); with a tracker, at most one identity read (none
with `--trusted-login`), one paginated comment read, at most
MAX_RESOLVE_COMPARES compare calls, one comment write per ticked key, and,
only when closing, two more paginated comment reads, one single-issue read
of the tracker right before the close (a reopen during the tick keeps it
open, reason `reopened`), at most one comment write and one issue PATCH (a
second PATCH only to reopen). Every pipeline reopen sends
`state_reason=reopened` explicitly. `upsert` adds one open-tracker listing,
and a PATCH only to reopen, after it posts an entry. The poller's
tracking-issue listing and the dependency-wait GraphQL return neither this
tracker nor its comments.

Output is one JSON line: `issue`, `url`, `created`, `entries`. Exit 0 on
success, 1 on bad arguments, 2 when a GitHub call failed, 3 when `upsert`
posted its entry but the open-tracker re-check after the post (or the reopen
it triggered) failed (output `{"error": ..., "posted": true}`).

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
`newly_parked`, `entries`, `overflow` and `parking`: the digest entry's
`parked=` stamp, the literal `overflow` when the item is only in the overflow
list or dropped count, or null on the disabled path. This module never talks
to Telegram.

At most one persisted CRITICAL alert attempt per parking (stuck-states P6b,
Refs #8291). The caller (unblock judge, poller project park) runs, after
every successful park that returns a `parking`:

  operator_step_issue.py needs-human alert-claim --repo OWNER/REPO --item N
      --parking STAMP|overflow [--trusted-login LOGIN]
  operator_step_issue.py needs-human alert-confirm --repo OWNER/REPO --item N
      --parking STAMP|overflow --comment-id ID --message-id ID

The caller first checks that Telegram can send at CRITICAL (no claim
otherwise). `alert-claim` reads the item's comments; a claim comment by the
pipeline login whose last non-empty line is
`<!-- ai:unblock-park-alert:v1 item=N parked=<STAMP|overflow> state=attempted|confirmed[ message=ID] -->`
for the same parking (or for `overflow`, which covers a later entry stamp)
means the attempt was already made: `claimed` false, reason
`already_attempted`. Otherwise it posts one `state=attempted` claim, re-reads
the comments, and wins only when its claim is the lowest-id matching trusted
claim (`lost_race` / `claim_unverified` otherwise). Only a winner sends, once,
with no retry; a numeric Telegram message id is then recorded in place with
`alert-confirm` (`state=confirmed message=ID`). Any write or re-read failure
exits 2 and the caller does not send: a lost page is preferred to a duplicate
one, and the digest stays the durable notice. The comment's `created_at` is
the attempt time. The `ai:unblock` prefix keeps the claim out of the unblock
scan's new-evidence check and out of the judge's round count. Each outcome
logs `NEEDS_HUMAN_ALERT item= parking= outcome=claimed|skip|confirmed|failed
reason=` on stderr. The kill switch below makes both a no-op.

Alert API budget (CLAUDE.md §15), per call: alert-claim makes at most one
identity read (none with --trusted-login), two paginated comment reads on the
item and one comment POST (one read and no write when already attempted);
alert-confirm makes one comment PATCH. The judge's run-start comment read is
minutes stale and the digest listing carries no item comments, so neither can
answer the claim question.
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


class TrackerRecheckError(ApiError):
	"""`upsert` posted its entry, but the open-tracker re-check after the post failed: exit 3.

	The entry is on the tracker; only whether the tracker is still open is
	unknown (or a reopen failed). A distinct exit code lets the caller warn
	accurately and keep its own follow-up (activation_verify.sh still posts its
	verdict comment) instead of treating the entry write as lost.
	"""


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
	# A non-integer number sorts first instead of raising ValueError, so tick's
	# guard reports it as a controlled ApiError (exit 2).
	return sorted(candidates, key=lambda issue: issue["number"] if type(issue.get("number")) is int else 0)


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
				# Only the line rendered from `dormant_until` may have the
				# dormant-line shape: resolve's _unticked_steps reads a step's
				# last indented line as its condition, so an instruction line
				# that looks like one is prefixed and can never pass for it.
				if DORMANT_LINE_RE.match(instruction_line.strip()):
					instruction_line = "Note: " + instruction_line.strip()
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


def _reopen_tracker_after_post(repo: str, number: int) -> None:
	"""Reopen the tracker when `resolve` closed it while this entry was posted.

	Stuck-states P10 close race: `resolve` re-reads the comments before and
	after its close PATCH, but an entry posted after that last read lands on a
	closed tracker that no later run lists. CLAUDE.md section 14 audit: the
	only state this writer already reads is the open-tracker listing, so it
	re-runs that listing once (no new endpoint) and reopens the tracker when
	it is no longer listed. Reopening is idempotent with `resolve`'s own
	reopen, and the reopened tracker is never closed automatically again.
	A failed listing or a failed reopen raises TrackerRecheckError (exit 3):
	the entry is already posted, but whether the tracker is still open is
	unknown, so the caller warns a person (activation_verify.sh sends its
	WARNING and still posts the verdict comment; the unblock judge records
	`ops_failed`) instead of silently stranding the entry on a closed tracker.
	The re-listing asks for 100 issues so a crowded label listing cannot hide
	an open tracker and trigger a needless reopen.
	"""
	try:
		listed = json.loads(_gh(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=100"]) or "[]")
	except (ApiError, ValueError) as exc:
		print(f"OPERATOR_STEP_RESOLVE op=upsert_recheck issue={number} outcome=error reason=listing_failed ({type(exc).__name__})", file=sys.stderr)
		raise TrackerRecheckError(f"operator-step entry posted, but the tracker re-listing after the post failed: {type(exc).__name__}") from exc
	if any(isinstance(issue, dict) and issue.get("number") == number for issue in (listed if isinstance(listed, list) else [])):
		return
	try:
		_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=open", "-f", "state_reason=reopened"])
	except ApiError as exc:
		print(f"OPERATOR_STEP_RESOLVE op=upsert_recheck issue={number} outcome=error reason=reopen_failed", file=sys.stderr)
		raise TrackerRecheckError(f"operator-step entry posted on closed tracker #{number}, and reopening it failed") from exc
	print(f"OPERATOR_STEP_RESOLVE op=upsert_recheck issue={number} outcome=reopened reason=closed_during_post", file=sys.stderr)


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
		number = existing.get("number")
		# Same guard as tick: a malformed number is a controlled ApiError (exit 2).
		if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
			raise ApiError("unreadable issue list: operator-step tracker has no issue number")
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
			_reopen_tracker_after_post(repo, number)
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
	number = candidates[0].get("number")
	if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
		raise ApiError("unreadable issue list: operator-step tracker has no issue number")
	result["issue"] = number
	latest = _authoritative_entry_comments(load_entry_comments(repo, number), login)
	for key in sorted(latest):
		body = str(latest[key].get("body") or "").replace("\r\n", "\n")
		lines = [line.strip() for line in body.split("\n")]
		source_match = SOURCE_SHA_RE.match(lines[1]) if len(lines) > 1 else None
		if any(DONE_RE.match(line) or RESOLVED_RE.match(line) for line in lines):
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


# --- resolving released entries (stuck-states P10) -----------------------------

# Keep in sync with RELEASE_CONDITION_RE in scripts/activation_verify.sh (a test checks parity).
RELEASE_CONDITION_RE = re.compile(r"^@?stable\s+contains\b", re.I)
RESOLVED_RE = re.compile(r"^<!-- ai:operator-step:resolved release=stable status=(ahead|identical) -->$")
RELEASE_STEP_TITLE_RE = re.compile(
	r"^(?:promote|tag|release|publish|cut|sync|propagate|roll out)\b[^\n]{0,200}?"
	r"(?:@stable|\bstable\b|ai update workflows|update[ _-]workflows|wrapper sync|consumer sync)",
	re.I,
)
RELEASE_ENTRY_KEY_RE = re.compile(r"^(?:pr|project)-[1-9][0-9]{0,9}$")
AUTO_CLOSED_MARKER = "<!-- ai:operator-step:auto-closed -->"
AUTO_CLOSED_TEXT = (
	"Every step listed here is done, so the poller closed this issue. "
	"A new operator step opens a new issue."
)
# A title-only release step (no dormant line) must not name something only a
# person can supply: "Publish the DEPLOY_TOKEN secret to stable" stays a human step.
# Letter-only lookarounds instead of `\b`: `_` is a regex word character, so
# `\b` never fires inside SCREAMING_SNAKE names such as `NPM_TOKEN` or `API_KEY`.
HUMAN_OBJECT_TITLE_RE = re.compile(
	r"(?<![A-Za-z])(?:secrets?|tokens?|credentials?|passwords?|variables?|settings?|permissions?|accounts?|keys?)(?![A-Za-z])",
	re.I,
)
MAX_RESOLVE_COMPARES = 10
RELEASE_REF = "stable"
UNTICKED_STEP_RE = re.compile(r"^- \[ \] \*\*(.*)\*\*$")
DORMANT_LINE_RE = re.compile(r"^Stays off until then: `(.*)`\.$")


def operator_step_auto_resolve_enabled() -> bool:
	return os.environ.get("OPERATOR_STEP_AUTO_RESOLVE_ENABLED", "true").strip().lower() != "false"


def _unticked_steps(body: str) -> list[tuple[str, str]]:
	"""(title, dormant text) per `- [ ] **Title**` line.

	render_entry writes the dormant line last in a step, so only the step's
	last indented line counts; an instruction line cannot stand in for it.
	"""
	steps: list[list] = []
	current: list | None = None
	for raw in str(body or "").replace("\r\n", "\n").split("\n"):
		if raw.startswith("- ["):
			match = UNTICKED_STEP_RE.match(raw.rstrip())
			current = [match.group(1), []] if match else None
			if current is not None:
				steps.append(current)
		elif current is not None and raw.startswith("  ") and raw.strip():
			current[1].append(raw.strip())
		elif current is not None and raw.strip():
			current = None
	result = []
	for title, indented in steps:
		dormant_match = DORMANT_LINE_RE.match(indented[-1]) if indented else None
		result.append((title, dormant_match.group(1) if dormant_match else ""))
	return result


def _is_release_entry(body: str) -> bool:
	"""True when at least one step is unticked and every unticked step is a release step."""
	steps = _unticked_steps(body)
	return bool(steps) and all(
		# The step's own dormant line decides; a release-verb title counts only
		# for a step that has no dormant line, so a human step such as "Sync the
		# DEPLOY_TOKEN secret to the stable environment" (dormant `DEPLOY_TOKEN`)
		# is never ticked. `dormant_until` is optional, so a title-only step also
		# must not name a secret, token, variable, setting or similar.
		RELEASE_CONDITION_RE.match(dormant)
		or (not dormant and RELEASE_STEP_TITLE_RE.match(title) and not HUMAN_OBJECT_TITLE_RE.search(title))
		for title, dormant in steps
	)


def _count_unticked(body: str) -> int:
	return sum(1 for line in str(body or "").replace("\r\n", "\n").split("\n") if line.startswith("- [ ]"))


def _release_status(repo: str, sha: str, cache: dict[str, str]) -> str:
	"""ahead | identical | behind | diverged | unknown for `<sha>...stable`; ApiError propagates."""
	if sha not in cache:
		raw = _gh(["api", f"repos/{repo}/compare/{sha}...{RELEASE_REF}", "--jq", ".status"]).strip()
		cache[sha] = raw if raw in ("ahead", "identical", "behind", "diverged") else "unknown"
	return cache[sha]


def _render_resolved(body: str, status: str, source_sha: str) -> str:
	lines = [
		"- [x]" + line[len("- [ ]"):] if line.startswith("- [ ]") else line
		for line in body.replace("\r\n", "\n").rstrip().split("\n")
	]
	lines += [
		"",
		f"<!-- ai:operator-step:resolved release={RELEASE_REF} status={status} -->",
		f"Done: the `{RELEASE_REF}` tag includes the merge `{source_sha[:7]}` (compare status `{status}`); ticked by the poller.",
	]
	return "\n".join(lines)


def _resolve_open_state(latest: dict[str, dict], legacy: list[tuple[str, str]], ticked: set[str]) -> tuple[int, int]:
	"""(unticked steps, entry count): authoritative comments first, legacy body sections otherwise."""
	unticked = 0
	for key, comment in latest.items():
		if key not in ticked:
			unticked += _count_unticked(str(comment.get("body") or ""))
	for key, text in legacy:
		if key not in latest:
			unticked += _count_unticked(text)
	return unticked, len(set(latest) | {key for key, _ in legacy})


def _resolve_log(**fields: object) -> None:
	print("OPERATOR_STEP_RESOLVE " + " ".join(f"{name}={value}" for name, value in fields.items()), file=sys.stderr)


def _tracker_still_closable(repo: str, number: int) -> bool:
	"""True when a fresh read shows the tracker open and not reopened.

	CLAUDE.md section 14 audit: the tick's only tracker read is the listing at
	its start, and the comment reads carry no issue state, so one single-issue
	GET right before the close is the only way to see a reopen made during the
	tick. An unreadable response raises ApiError, so nothing is closed.
	"""
	try:
		current = json.loads(_gh(["api", f"repos/{repo}/issues/{number}"]) or "{}")
	except ValueError as exc:
		raise ApiError(f"unreadable operator-step tracker: {exc}") from exc
	if not isinstance(current, dict):
		raise ApiError("unreadable operator-step tracker: expected an issue object")
	return current.get("state") == "open" and str(current.get("state_reason") or "") != "reopened"


def resolve(repo: str, trusted_login: str | None = None, max_compares: int = MAX_RESOLVE_COMPARES) -> dict:
	if not REPO_RE.match(repo):
		raise UsageError(f"--repo must be OWNER/REPO, got {repo!r}")
	result: dict = {"issue": None, "ticked": [], "skipped": {}, "compares": 0, "unticked": None, "closed": False}
	if not operator_step_auto_resolve_enabled():
		_resolve_log(op="resolve", outcome="skip", reason="disabled")
		result["reason"] = "disabled"
		return result
	listing = _gh(["api", f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=30"])
	try:
		issues = json.loads(listing or "[]")
	except ValueError as exc:
		raise ApiError(f"unreadable issue list: {exc}") from exc
	candidates = find_issues(issues)
	if not candidates:
		return result
	tracker = candidates[0]
	number = tracker.get("number")
	if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
		raise ApiError("unreadable issue list: operator-step tracker has no issue number")
	result["issue"] = number
	login = (trusted_login or "").strip() or _gh(["api", "user", "--jq", ".login"]).strip()
	if not login:
		raise ApiError("operator-step resolve identity is unavailable")
	latest = _authoritative_entry_comments(load_entry_comments(repo, number), login)
	cache: dict[str, str] = {}
	halted = False
	resolved: set[str] = set()
	for key in sorted(latest, key=lambda entry_key: latest[entry_key]["id"]):
		body = str(latest[key].get("body") or "").replace("\r\n", "\n")
		raw_lines = body.split("\n")
		lines = [line.strip() for line in raw_lines]
		source_match = SOURCE_SHA_RE.match(lines[1]) if len(lines) > 1 else None
		status = "-"
		if any(DONE_RE.match(line) or RESOLVED_RE.match(line) for line in lines) or not any(line.startswith("- [ ]") for line in raw_lines):
			outcome = "already_done"
		elif not RELEASE_ENTRY_KEY_RE.match(key) or not _is_release_entry(body):
			outcome = "not_release"
		elif not source_match:
			outcome = "no_source_sha"
		else:
			sha = source_match.group(1)
			if halted or (sha not in cache and result["compares"] >= max_compares):
				outcome = "deferred"
			else:
				try:
					if sha not in cache:
						result["compares"] += 1
					status = _release_status(repo, sha, cache)
				except ApiError:
					# Fail closed: nothing is ticked on an unreadable compare, and
					# the rest of this run's compares are skipped.
					halted = True
					outcome = "compare_failed"
				else:
					outcome = "ticked" if status in ("ahead", "identical") else "not_contained"
		if outcome == "ticked":
			_gh(["api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={_render_resolved(body, status, sha)}"])
			result["ticked"].append(key)
			resolved.add(key)
		else:
			result["skipped"][outcome] = result["skipped"].get(outcome, 0) + 1
		_resolve_log(key=key, outcome=outcome, status=status)
	legacy = parse_entries(str(tracker.get("body") or ""))
	unticked, entry_count = _resolve_open_state(latest, legacy, resolved)
	result["unticked"] = unticked
	if entry_count == 0:
		reason = "no_entries"
	elif unticked > 0:
		reason = "unticked"
	elif str(tracker.get("state_reason") or "") == "reopened":
		# A person (or the pipeline's close-race reopen) reopened the tracker:
		# never close it again automatically.
		reason = "reopened"
	else:
		recheck_comments = load_entry_comments(repo, number)
		recheck_latest = _authoritative_entry_comments(recheck_comments, login)
		recheck_unticked, _ = _resolve_open_state(recheck_latest, legacy, set())
		if recheck_unticked != 0 or set(recheck_latest) - set(latest):
			reason = "recheck_changed"
		elif not _tracker_still_closable(repo, number):
			# The listing above is minutes of round trips old: a person who
			# closed and reopened the tracker meanwhile keeps it open.
			reason = "reopened"
		else:
			newest_entry = max((comment["id"] for comment in recheck_latest.values()), default=0)
			already_posted = any(
				isinstance(comment.get("id"), int) and comment["id"] > newest_entry
				and (comment.get("user") if isinstance(comment.get("user"), dict) else {}).get("login") == login
				and str(comment.get("body") or "").replace("\r\n", "\n").split("\n", 1)[0].strip() == AUTO_CLOSED_MARKER
				for comment in recheck_comments
			)
			if not already_posted:
				_gh(["api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={AUTO_CLOSED_MARKER}\n{AUTO_CLOSED_TEXT}"])
			# Known residual: the fresh read above and this PATCH are not atomic
			# (the issues API has no conditional update), so a person who reopens
			# the tracker in the gap between them is overwritten by this close.
			_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=closed", "-f", "state_reason=completed"])
			result["closed"] = True
			reason = "closed"
			# The re-read above is not atomic with the close: an entry written
			# in between would sit on a closed tracker no later run lists. One
			# more read after the close reopens it, and upsert's own re-listing
			# (_reopen_tracker_after_post) covers a post whose listing is read
			# after this close. This narrows the window; it does not close it:
			# a post whose re-listing ran before the close PATCH and that lands
			# after this read is not seen by either check.
			try:
				after_close = _authoritative_entry_comments(load_entry_comments(repo, number), login)
			except ApiError:
				# Fail safe: without the read, a racing entry cannot be ruled
				# out, so reopen the tracker before reporting the error.
				try:
					_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=open", "-f", "state_reason=reopened"])
				except ApiError:
					_resolve_log(op="close", issue=number, outcome="closed", reason="after_close_reopen_failed")
					raise
				_resolve_log(op="close", issue=number, outcome="open", reason="after_close_read_failed")
				raise
			if any(key not in recheck_latest or comment["id"] != recheck_latest[key]["id"] for key, comment in after_close.items()):
				_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=open", "-f", "state_reason=reopened"])
				result["closed"] = False
				reason = "reopened_after_close"
	_resolve_log(op="close", issue=number, outcome="closed" if result["closed"] else "open", reason=reason)
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
_NEEDS_HUMAN_PARKING_PATTERN = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|overflow"
NEEDS_HUMAN_PARKING_RE = re.compile(rf"^(?:{_NEEDS_HUMAN_PARKING_PATTERN})$")
NEEDS_HUMAN_ALERT_MARKER_RE = re.compile(
	rf"^<!-- ai:unblock-park-alert:v1 item=([1-9][0-9]{{0,9}}) parked=({_NEEDS_HUMAN_PARKING_PATTERN}) "
	r"state=(attempted|confirmed)(?: message=([1-9][0-9]{0,19}))? -->$"
)
NEEDS_HUMAN_ALERT_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
NEEDS_HUMAN_ALERT_TEXT = "Needs-human alert attempt recorded for this parking; the needs-human digest lists this item."
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
		return {"issue": None, "url": "", "item": item, "outcome": "skip", "reason": "disabled", "newly_parked": False,
			"parking": None}
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
				"newly_parked": True, "entries": 1, "overflow": 0, "parking": stamp}
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
	# The parking identity comes from the rendered body: an entry the body
	# limit spilled into overflow is an overflow parking (prune later promotes
	# it with a fresh stamp, which an `overflow` alert claim still covers).
	rendered_entries = parse_digest(new_body)[0]
	parking = rendered_entries[item]["parked"] if item in rendered_entries else "overflow"
	return {"issue": int(digest["number"]) if digest else None, "url": (digest or {}).get("html_url", ""), "item": item,
		"outcome": outcome, "newly_parked": bool(newly_parked), "entries": len(entries), "overflow": len(overflow),
		"parking": parking}


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


# --- at most one persisted alert attempt per parking (stuck-states P6b) -------


def _needs_human_alert_log(item: object, parking: object, outcome: str, reason: str) -> None:
	safe_parking = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(parking))[:40] or "none"
	safe_reason = re.sub(r"[^A-Za-z0-9_.:-]", "_", reason)[:80] or "none"
	print(f"NEEDS_HUMAN_ALERT item={item} parking={safe_parking} outcome={outcome} reason={safe_reason}", file=sys.stderr)


def _validate_alert_args(repo: str, item: object, parking: object) -> None:
	if not REPO_RE.match(repo or ""):
		raise UsageError(f"--repo must be OWNER/REPO, got {repo!r}")
	if not isinstance(item, int) or isinstance(item, bool) or item < 1:
		raise UsageError("--item must be a positive issue or PR number")
	if not isinstance(parking, str) or not NEEDS_HUMAN_PARKING_RE.match(parking):
		raise UsageError(f"--parking must be an ISO UTC stamp (YYYY-MM-DDTHH:MM:SSZ) or 'overflow', got {parking!r}")


def render_alert_claim(item: int, parking: str, state: str, message_id: str | None = None) -> str:
	message = f" message={message_id}" if message_id else ""
	return f"{NEEDS_HUMAN_ALERT_TEXT}\n\n<!-- ai:unblock-park-alert:v1 item={item} parked={parking} state={state}{message} -->"


def alert_claims(comments: object, trusted_login: str, item: int, parking: str) -> list[dict]:
	"""Trusted claims for this parking, lowest comment id first.

	Only the pipeline login counts, and only when the marker is the last
	non-empty line with the same item. An `overflow` claim covers every
	parking of the item (a promoted overflow item gets a fresh stamp)."""
	found: list[dict] = []
	for comment in comments if isinstance(comments, list) else []:
		if not isinstance(comment, dict):
			continue
		user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
		comment_id = comment.get("id")
		if (not trusted_login or user.get("login") != trusted_login
				or not isinstance(comment_id, int) or isinstance(comment_id, bool)):
			continue
		lines = [line.strip() for line in str(comment.get("body") or "").replace("\r\n", "\n").split("\n") if line.strip()]
		match = NEEDS_HUMAN_ALERT_MARKER_RE.match(lines[-1]) if lines else None
		if not match or int(match.group(1)) != item:
			continue
		if match.group(2) != parking and match.group(2) != "overflow":
			continue
		found.append({"id": comment_id, "parked": match.group(2), "state": match.group(3), "message": match.group(4)})
	return sorted(found, key=lambda claim: claim["id"])


def needs_human_alert_claim(repo: str, item: int, parking: str, trusted_login: str | None = None,
		now: str | None = None) -> dict:
	_validate_alert_args(repo, item, parking)
	result: dict = {"item": item, "parking": parking, "claimed": False}
	if not needs_human_enabled():
		_needs_human_alert_log(item, parking, "skip", "disabled")
		return dict(result, reason="disabled")
	login = _resolve_login(trusted_login)
	existing = alert_claims(load_entry_comments(repo, item), login, item, parking)
	if existing:
		_needs_human_alert_log(item, parking, "skip", "already_attempted")
		return dict(result, reason="already_attempted", state=existing[0]["state"], comment_id=existing[0]["id"])
	try:
		raw = _gh(["api", f"repos/{repo}/issues/{item}/comments", "-f", f"body={render_alert_claim(item, parking, 'attempted')}"])
		created = json.loads(raw or "{}")
	except (ApiError, ValueError) as exc:
		_needs_human_alert_log(item, parking, "failed", "claim_write_failed")
		raise ApiError(f"needs-human alert claim write failed: {exc}") from exc
	comment_id = created.get("id") if isinstance(created, dict) else None
	if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
		_needs_human_alert_log(item, parking, "failed", "claim_write_failed")
		raise ApiError("needs-human alert claim write returned no comment id")
	try:
		claims = alert_claims(load_entry_comments(repo, item), login, item, parking)
	except ApiError:
		_needs_human_alert_log(item, parking, "failed", "claim_reread_failed")
		raise
	claim_ids = [claim["id"] for claim in claims]
	if comment_id not in claim_ids:
		_needs_human_alert_log(item, parking, "skip", "claim_unverified")
		return dict(result, reason="claim_unverified", comment_id=comment_id)
	if claim_ids[0] != comment_id:
		_needs_human_alert_log(item, parking, "skip", "lost_race")
		return dict(result, reason="lost_race", comment_id=comment_id)
	attempted_at = now or (created.get("created_at") if isinstance(created.get("created_at"), str) else "") or _now_iso()
	_needs_human_alert_log(item, parking, "claimed", "won")
	return dict(result, claimed=True, comment_id=comment_id, alert_attempted_at=attempted_at)


def needs_human_alert_confirm(repo: str, item: int, parking: str, comment_id: str, message_id: str) -> dict:
	_validate_alert_args(repo, item, parking)
	for flag, value in (("--comment-id", comment_id), ("--message-id", message_id)):
		if not isinstance(value, str) or not NEEDS_HUMAN_ALERT_ID_RE.match(value):
			raise UsageError(f"{flag} must be a positive decimal id of at most 20 digits, got {value!r}")
	result: dict = {"item": item, "parking": parking, "comment_id": int(comment_id), "confirmed": False}
	if not needs_human_enabled():
		_needs_human_alert_log(item, parking, "skip", "disabled")
		return dict(result, reason="disabled")
	body = render_alert_claim(item, parking, "confirmed", message_id)
	try:
		_gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "-f", f"body={body}"])
	except ApiError:
		_needs_human_alert_log(item, parking, "failed", "confirm_write_failed")
		raise
	_needs_human_alert_log(item, parking, "confirmed", "sent")
	return dict(result, confirmed=True, message_id=message_id)


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
	resolve_cmd = sub.add_parser("resolve")
	resolve_cmd.add_argument("--repo", required=True)
	resolve_cmd.add_argument("--trusted-login", default="")
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
	claim_cmd = digest_sub.add_parser("alert-claim")
	claim_cmd.add_argument("--repo", required=True)
	claim_cmd.add_argument("--item", required=True, type=int)
	claim_cmd.add_argument("--parking", required=True)
	claim_cmd.add_argument("--trusted-login", default="")
	confirm_cmd = digest_sub.add_parser("alert-confirm")
	confirm_cmd.add_argument("--repo", required=True)
	confirm_cmd.add_argument("--item", required=True, type=int)
	confirm_cmd.add_argument("--parking", required=True)
	confirm_cmd.add_argument("--comment-id", required=True)
	confirm_cmd.add_argument("--message-id", required=True)
	args = parser.parse_args(argv)
	try:
		if args.command == "needs-human" and args.digest_command == "park":
			result = needs_human_park(args.repo, args.item, args.kind, args.stop, args.reason, args.link,
				args.trusted_login or None, args.item_labeled)
		elif args.command == "needs-human" and args.digest_command == "alert-claim":
			result = needs_human_alert_claim(args.repo, args.item, args.parking, args.trusted_login or None)
		elif args.command == "needs-human" and args.digest_command == "alert-confirm":
			result = needs_human_alert_confirm(args.repo, args.item, args.parking, args.comment_id, args.message_id)
		elif args.command == "needs-human" and args.digest_command == "prune":
			result = needs_human_prune(args.repo, args.trusted_login or None)
		elif args.command == "resolve":
			result = resolve(args.repo, args.trusted_login or None)
		elif args.command == "tick":
			result = tick(args.repo, args.stable_sha, args.repo_dir, args.trusted_login or None)
		else:
			result = upsert(args.repo, args.key, args.source, load_steps(args.steps_file), args.source_sha or None)
	except UsageError as exc:
		print(json.dumps({"error": str(exc)}))
		return 1
	except TrackerRecheckError as exc:
		# Before ApiError: the entry was posted; only the tracker re-check failed.
		print(json.dumps({"error": str(exc), "posted": True}))
		return 3
	except ApiError as exc:
		print(json.dumps({"error": str(exc)}))
		return 2
	print(json.dumps(result))
	return 0


if __name__ == "__main__":
	sys.exit(main())
