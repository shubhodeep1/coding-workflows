#!/usr/bin/env python3
"""Check after merge (issue #8427, phase P5 of
docs/plans/persistent-unattended-workflows-plan.md).

When a standalone issue's PR merges into the default branch and the approved
plan's done-check names a post-merge workflow
(`ACCEPTANCE: post-merge workflow <.github/workflows/f.yml> [job <name>]`,
scripts/acceptance_check.py), `arm` posts a pending marker comment and adds
the `ai:verify-pending` label. On every poller tick `check` finds the first
completed run of that workflow on the base branch that contains the merge
and grades it:

	held        the run (or the named job) succeeded: label removed
	recurred    it failed: the issue is reopened in ai:planning (or
			ai:needs-human past PERSIST_MAX_APPROACHES re-plans) and the
			poller posts the existing retrigger_plan `/answer` comment
	unverified  no such run within PERSIST_POSTMERGE_WINDOW_HOURS: label removed
	wait        nothing to grade yet

Markers (append-only; never edited), one per comment, on their own line:

	<!-- AI_POSTMERGE_VERIFY_V1 {"pr":N,"merge_sha":"40hex","workflow":"...",
	"job":str|null,"branch":"...","merged_at":"...Z","state":"pending|recurred|unverified"} -->

Trust: a marker counts only in a comment authored by the pipeline login whose
first line is the header of its state (`### Check after merge pending`,
`## Check after merge failed`, `### Check after merge unverified`). An
unknown login trusts nothing; a label without a trusted active marker is
removed and nothing is reopened.

Switches (each defaulted here, unattended instructions section 8):
	PERSIST_POSTMERGE_VERIFY_ENABLED  default false. Off: `arm` makes no call;
			`check` only removes leftover labels whose
			window has passed (one list read per tick).
	PERSIST_POSTMERGE_WINDOW_HOURS    default 24, clamped to 1..168
	PERSIST_POSTMERGE_MAX_PER_TICK    default 3, clamped to 1..10
	PERSIST_MAX_APPROACHES            default 3, clamped to 1..20 (fallback cap)
	PERSIST_BUDGET_ENABLED            default false (P6 owns the re-plan when on
			and scripts/persist_budget.py exists)
	WORKFLOW_HEAL_VERIFY_ENABLED      default false (#8409 P7 owns heal issues)

CLI (always exits 0):
	arm --repo R --issue N --pr N --merge-sha S --merged-at T --base B
		[--pr-labels-json J] [--trusted-login L]
	check --repo R --out F [--runs-file F] [--trusted-login L]

Log lines (stderr, tokens only):
	PERSIST postmerge outcome=<armed|ok|held|pending|recurred|unverified|cleaned|skipped|error> reason=<token> [issue=<n>]

API budget (section 14): `arm` makes at most 1 identity read (cached by
attempt_journal.resolve_login), 1 issue read (only with
WORKFLOW_HEAL_VERIFY_ENABLED=true), 1 paginated comments read and 3 writes
(comment, label, journal). `check` makes 1 list read per tick plus, per
candidate (at most MAX_PER_TICK), 1 comments read, 1 workflow-runs read (only
when the poller's per-tick runs cache, which keeps only the 20 newest
completed runs repo-wide, has no match), 1 compare read (only when the run's
head is not the merge commit), 1 jobs read (only for a named job) and at most
4 writes. Audited existing calls: the poller holds no comments of closed
issues and no per-workflow run history, so these reads are the minimum.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
	import attempt_journal  # noqa: E402
except Exception:  # noqa: BLE001 - fail open below
	attempt_journal = None  # type: ignore[assignment]

MARKER = "AI_POSTMERGE_VERIFY_V1"
LABEL = "ai:verify-pending"
LABEL_ENC = "ai%3Averify-pending"
HEAL_LABEL = "ai:workflow-heal"
SKIP_LABEL = "ai:skip-activation-verify"
PLANNING_LABEL = "ai:planning"
NEEDS_HUMAN_LABEL = "ai:needs-human"
ORCHESTRATOR_METADATA = "**Orchestrator metadata**"
HEADER_PENDING = "### Check after merge pending"
HEADER_FAILED = "## Check after merge failed"
HEADER_UNVERIFIED = "### Check after merge unverified"
HEADER_BY_STATE = {"pending": HEADER_PENDING, "recurred": HEADER_FAILED, "unverified": HEADER_UNVERIFIED}
STATES = tuple(HEADER_BY_STATE)
# Mirrors _AI_PHASE_LABELS in scripts/label_helpers.sh (a test checks parity).
PHASE_LABELS = (
	"ai:done", "ai:implementing", "ai:awaiting-approval", "ai:planning", "ai:clarification",
	"ai:validating", "ai:validated", "ai:validation-failed", "ai:validation-fixing", "ai:validation-recovery",
	"ai:security-pass", "ai:security-pass-fixing", "ai:security-pass-failed", "ai:ready-to-merge",
	"ai:needs-human", "ai:blocked", "ai:review-blocked", "ai:implementation-failed", "ai:merged", "ai:closed",
)
IGNORED_CONCLUSIONS = ("cancelled", "skipped", "neutral", "stale", "action_required")
FAIL_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
JOB_MAX = 100

MARKER_LINE_RE = re.compile(r"^<!-- " + MARKER + r" (\{.*\}) -->$")
WORKFLOW_PATH_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
URL_RE = re.compile(r"^https://[A-Za-z0-9./_-]{1,300}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
TOKEN_RE = re.compile(r"[^A-Za-z0-9_.-]")


def log(outcome: str, reason: str, issue: Any = None) -> None:
	suffix = f" issue={issue}" if issue not in (None, "") else ""
	print(f"PERSIST postmerge outcome={outcome} reason={reason}{suffix}", file=sys.stderr)


def _flag(name: str) -> bool:
	return os.environ.get(name, "false").strip().lower() == "true"


def _int_env(name: str, default: int, low: int, high: int) -> int:
	raw = os.environ.get(name, "").strip()
	try:
		value = int(raw) if raw else default
	except ValueError:
		value = default
	return max(low, min(high, value))


def enabled() -> bool:
	return _flag("PERSIST_POSTMERGE_VERIFY_ENABLED")


def window_hours() -> int:
	return _int_env("PERSIST_POSTMERGE_WINDOW_HOURS", 24, 1, 168)


def max_per_tick() -> int:
	return _int_env("PERSIST_POSTMERGE_MAX_PER_TICK", 3, 1, 10)


def max_approaches() -> int:
	return _int_env("PERSIST_MAX_APPROACHES", 3, 1, 20)


def _now() -> datetime:
	return datetime.now(timezone.utc)


def parse_ts(value: Any) -> datetime | None:
	if not isinstance(value, str) or not TS_RE.match(value):
		return None
	try:
		return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
	except ValueError:
		return None


def _token(value: Any, limit: int = 40) -> str:
	return TOKEN_RE.sub("_", str(value if value is not None else ""))[:limit] or "-"


def _valid_branch(value: Any) -> bool:
	return isinstance(value, str) and bool(BRANCH_RE.match(value)) and ".." not in value


def _valid_job(value: Any) -> bool:
	return isinstance(value, str) and 1 <= len(value) <= JOB_MAX and not CONTROL_RE.search(value)


# --- markers ------------------------------------------------------------------


def marker_line(obj: dict) -> str:
	text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
	# The JSON can never close the HTML comment: no literal < or > remains.
	text = text.replace("<", "\\u003c").replace(">", "\\u003e")
	return f"<!-- {MARKER} {text} -->"


def normalise_marker(raw: Any) -> dict | None:
	"""A marker with every field validated, or None."""
	if not isinstance(raw, dict):
		return None
	pr = raw.get("pr")
	if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
		return None
	merge_sha = raw.get("merge_sha")
	if not isinstance(merge_sha, str) or not SHA_RE.match(merge_sha):
		return None
	workflow = raw.get("workflow")
	if not isinstance(workflow, str) or not WORKFLOW_PATH_RE.match(workflow):
		return None
	job = raw.get("job")
	if job is not None and not _valid_job(job):
		return None
	branch = raw.get("branch")
	if not _valid_branch(branch):
		return None
	merged_at = raw.get("merged_at")
	if parse_ts(merged_at) is None:
		return None
	state = raw.get("state")
	if state not in STATES:
		return None
	return {"pr": pr, "merge_sha": merge_sha, "workflow": workflow, "job": job, "branch": branch,
		"merged_at": merged_at, "state": state}


def parse_markers(comments: Any, login: str) -> list[dict]:
	"""Trusted markers in comment order (oldest first)."""
	if attempt_journal is None or not isinstance(comments, list):
		return []
	trusted = attempt_journal._norm_login(login)
	if not trusted:
		return []
	out: list[dict] = []
	for comment in comments:
		if not isinstance(comment, dict):
			continue
		body = comment.get("body")
		if not isinstance(body, str) or MARKER not in body:
			continue
		if attempt_journal._norm_login(attempt_journal._comment_author(comment)) != trusted:
			continue
		lines = [line.rstrip("\r") for line in body.lstrip("\ufeff").split("\n")]
		header = lines[0] if lines else ""
		for line in lines[1:]:
			match = MARKER_LINE_RE.match(line)
			if not match:
				continue
			try:
				marker = normalise_marker(json.loads(match.group(1)))
			except ValueError:
				marker = None
			# The header must name the marker's own state.
			if marker is not None and HEADER_BY_STATE[marker["state"]] == header:
				out.append(marker)
			break
	return out


def active_marker(markers: list[dict]) -> dict | None:
	"""The newest pending marker, unless a later outcome marker closed it."""
	for index in range(len(markers) - 1, -1, -1):
		marker = markers[index]
		if marker["state"] != "pending":
			continue
		for later in markers[index + 1:]:
			if later["state"] != "pending" and later["pr"] == marker["pr"] and later["merge_sha"] == marker["merge_sha"]:
				return None
		return marker
	return None


def replan_count(markers: list[dict]) -> int:
	return sum(1 for m in markers if m["state"] == "recurred")


# --- grading ------------------------------------------------------------------


def _run_path(run: dict) -> str:
	path = run.get("path")
	return path.split("@", 1)[0] if isinstance(path, str) else ""


def select_run(runs: Any, marker: dict) -> dict | None:
	"""Oldest completed run of the marker's workflow on its branch created at
	or after the merge; a run on the merge commit itself wins."""
	if isinstance(runs, dict):
		runs = runs.get("workflow_runs")
	if not isinstance(runs, list):
		return None
	merged = parse_ts(marker.get("merged_at"))
	if merged is None:
		return None
	kept: list[tuple[datetime, int, dict]] = []
	for run in runs:
		if not isinstance(run, dict) or run.get("status") != "completed":
			continue
		if _run_path(run) != marker["workflow"] or run.get("head_branch") != marker["branch"]:
			continue
		created = parse_ts(run.get("created_at"))
		if created is None or created < merged:
			continue
		if run.get("conclusion") in IGNORED_CONCLUSIONS:
			continue
		run_id = run.get("id")
		if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
			continue
		if not isinstance(run.get("head_sha"), str) or not SHA_RE.match(run["head_sha"]):
			continue
		kept.append((created, run_id, run))
	kept.sort(key=lambda item: (item[0], item[1]))
	for _, _, run in kept:
		if run["head_sha"] == marker["merge_sha"]:
			return run
	return kept[0][2] if kept else None


def job_verdict(jobs: Any, job: str | None) -> tuple[str | None, str]:
	"""(held|recurred|None, failing job name). Matches `name == job` or a
	matrix name starting with `job + " ("`."""
	if isinstance(jobs, dict):
		jobs = jobs.get("jobs")
	if not job or not isinstance(jobs, list):
		return None, ""
	matched = [j for j in jobs if isinstance(j, dict) and isinstance(j.get("name"), str)
		and (j["name"] == job or j["name"].startswith(job + " ("))]
	if not matched:
		return None, ""
	for item in matched:
		if item.get("conclusion") in FAIL_CONCLUSIONS:
			return "recurred", item["name"]
	if all(item.get("conclusion") == "success" for item in matched):
		return "held", ""
	return None, ""


def decide(marker: dict, run: dict | None, contained: bool, verdict: str | None, now: datetime, window_h: int) -> str:
	"""held, recurred, unverified or wait."""
	if run is not None and contained:
		if verdict is None:
			conclusion = run.get("conclusion")
			if conclusion == "success":
				verdict = "held"
			elif conclusion in FAIL_CONCLUSIONS:
				verdict = "recurred"
		if verdict in ("held", "recurred"):
			return verdict
	merged = parse_ts(marker.get("merged_at"))
	if merged is None or now >= merged + timedelta(hours=window_h):
		return "unverified"
	return "wait"


def labels_after_reopen(current: Any, target_phase: str) -> list[str]:
	drop = set(PHASE_LABELS) | {LABEL}
	out: list[str] = []
	for label in current if isinstance(current, list) else []:
		name = label.get("name") if isinstance(label, dict) else label
		if isinstance(name, str) and name not in drop and name not in out:
			out.append(name)
	out.append(target_phase)
	return out


# --- comments -----------------------------------------------------------------


def _visible(text: Any) -> str:
	clean = " ".join(CONTROL_RE.sub(" ", str(text)).split()).replace("`", "'")
	return attempt_journal._scrub(clean) if attempt_journal is not None else clean


def _target_text(marker: dict) -> str:
	text = f"`{marker['workflow']}`"
	if marker.get("job"):
		text += f" job `{_visible(marker['job'])}`"
	return text


def pending_comment(marker: dict, window_h: int) -> str:
	return "\n".join([
		HEADER_PENDING, "",
		f"PR #{marker['pr']} merged into `{marker['branch']}` at {marker['merged_at']}. The pipeline grades the first "
		f"completed run of {_target_text(marker)} that contains the merge, for up to {window_h} h.", "",
		marker_line(marker), "",
	])


def failed_comment(marker: dict, run: dict, failing_job: str, next_step: str) -> str:
	url = run.get("html_url") if isinstance(run.get("html_url"), str) and URL_RE.match(run["html_url"]) else ""
	where = f" in run {url}" if url else ""
	job_text = f" (failing job: `{_visible(failing_job)}`)" if failing_job else ""
	return "\n".join([
		HEADER_FAILED, "",
		f"The check after merge for PR #{marker['pr']} failed: {_target_text(marker)} concluded "
		f"`{_token(run.get('conclusion'))}`{where}{job_text}.", "",
		f"Next: {next_step}", "",
		marker_line(dict(marker, state="recurred")), "",
	])


def unverified_comment(marker: dict, window_h: int) -> str:
	return "\n".join([
		HEADER_UNVERIFIED, "",
		f"The check after merge for PR #{marker['pr']} is unverified (no run of `{marker['workflow']}` "
		f"within {window_h} h).", "",
		marker_line(dict(marker, state="unverified")), "",
	])


# --- host side (gh) -----------------------------------------------------------


def _gh(args: list[str]) -> bytes | None:
	"""One logical gh api call (attempt_journal._gh: gh_api_retry, optional)."""
	if attempt_journal is None:
		return None
	return attempt_journal._gh(args)


def _gh_json(args: list[str]) -> Any:
	raw = _gh(args)
	if raw is None:
		return None
	try:
		return json.loads(raw.decode("utf-8", "replace") or "null")
	except ValueError:
		return None


def _load_comments(repo: str, issue: int) -> list | None:
	raw = _gh(["--paginate", f"repos/{repo}/issues/{issue}/comments?per_page=100"])
	return attempt_journal._decode_pages(raw) if raw is not None else None


def _remove_label(repo: str, issue: int) -> bool:
	return _gh([f"repos/{repo}/issues/{issue}/labels/{LABEL_ENC}", "-X", "DELETE"]) is not None


def _post_comment(repo: str, issue: int, body: str) -> bool:
	return _gh([f"repos/{repo}/issues/{issue}/comments", "-X", "POST", "-f", f"body={body}"]) is not None


def _journal(repo: str, issue: int, outcome: str, comments: list, login: str, note: str,
		failure: tuple[str, str, str] | None = None) -> None:
	"""One attempt-journal entry (stage postmerge), reusing the comments read."""
	if attempt_journal is None or not attempt_journal.enabled():
		return
	runtime = os.environ.get("RUNTIME_DIR", "")
	work = tempfile.mkdtemp(prefix="persist_postmerge.", dir=runtime if runtime and os.path.isdir(runtime) else None)
	try:
		comments_file = Path(work) / "comments.json"
		note_file = Path(work) / "note.txt"
		comments_file.write_text(json.dumps(comments), encoding="utf-8")
		note_file.write_text(note, encoding="utf-8")
		argv = ["append", "--repo", repo, "--issue", str(issue), "--stage", "postmerge", "--outcome", outcome,
			"--note-file", str(note_file), "--comments-file", str(comments_file), "--trusted-login", login]
		if failure is not None:
			text_file = Path(work) / "failure.txt"
			text_file.write_text(failure[2], encoding="utf-8")
			argv += ["--failure-workflow", failure[0], "--failure-step", failure[1], "--failure-text-file", str(text_file)]
		attempt_journal.main(argv)
	finally:
		shutil.rmtree(work, ignore_errors=True)


# --- arm ----------------------------------------------------------------------


def _str_labels(raw: Any) -> list[str]:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw or "[]")
		except ValueError:
			return []
	if not isinstance(raw, list):
		return []
	out = []
	for item in raw:
		name = item.get("name") if isinstance(item, dict) else item
		if isinstance(name, str):
			out.append(name)
	return out


def _positive(value: Any) -> int | None:
	text = str(value or "")
	return int(text) if text.isdigit() and int(text) > 0 else None


def cmd_arm(args: argparse.Namespace) -> None:
	issue = _positive(args.issue)
	if not enabled():
		log("skipped", "disabled", issue)
		return
	if SKIP_LABEL in _str_labels(args.pr_labels_json):
		log("skipped", "skip_activation_verify", issue)
		return
	pr = _positive(args.pr)
	if (not REPO_RE.match(args.repo or "") or issue is None or pr is None or not SHA_RE.match(args.merge_sha or "")
			or parse_ts(args.merged_at) is None or not _valid_branch(args.base)):
		log("skipped", "invalid_input", issue)
		return
	try:
		import acceptance_check  # noqa: PLC0415
	except Exception:  # noqa: BLE001
		log("error", "acceptance_check_unavailable", issue)
		return
	login = attempt_journal.resolve_login(args.trusted_login)
	if not login:
		log("error", "identity_unavailable", issue)
		return
	if _flag("WORKFLOW_HEAL_VERIFY_ENABLED"):
		data = _gh_json([f"repos/{args.repo}/issues/{issue}"])
		if not isinstance(data, dict):
			log("error", "gh_read", issue)
			return
		if HEAL_LABEL in _str_labels(data.get("labels")):
			log("skipped", "heal_owned", issue)
			return
	comments = _load_comments(args.repo, issue)
	if comments is None:
		log("error", "gh_read", issue)
		return
	body = acceptance_check._plan_comment(comments, login)
	done = acceptance_check.parse_marker(body) if body else {"state": "none"}
	post = done.get("post_merge") if done.get("state") == "valid" else None
	if not post:
		_journal(args.repo, issue, "ok", comments, login, "check after merge: no post-merge workflow")
		log("ok", "no_postmerge_workflow", issue)
		return
	marker = normalise_marker({"pr": pr, "merge_sha": args.merge_sha, "workflow": post.get("workflow"),
		"job": post.get("job") or None, "branch": args.base, "merged_at": args.merged_at, "state": "pending"})
	if marker is None:
		log("skipped", "invalid_input", issue)
		return
	existing = active_marker(parse_markers(comments, login))
	already = existing is not None and existing["pr"] == pr and existing["merge_sha"] == marker["merge_sha"]
	# Label first: `check` finds candidates by label, so a marker without the
	# label would never be graded. The add is idempotent, so retry it once.
	label_args = [f"repos/{args.repo}/issues/{issue}/labels", "-X", "POST", "-f", f"labels[]={LABEL}"]
	if _gh(label_args) is None and _gh(label_args) is None:
		log("error", "label_write", issue)
		return
	if not already and not _post_comment(args.repo, issue, pending_comment(marker, window_hours())):
		# A label without a trusted marker is removed by the next `check`.
		log("error", "comment_write", issue)
		return
	if not already:
		_journal(args.repo, issue, "ok", comments, login, f"check after merge armed: {marker['workflow']}")
	log("armed", "already_armed" if already else "postmerge_workflow", issue)


# --- check --------------------------------------------------------------------


def _write_out(path: str, obj: dict) -> None:
	try:
		Path(path).write_text(json.dumps(obj, sort_keys=True) + "\n", encoding="utf-8")
	except OSError:
		log("error", "out_unwritable")


def _load_runs_file(path: str | None) -> Any:
	if not path:
		return None
	try:
		return json.loads(Path(path).read_text(encoding="utf-8") or "null")
	except (OSError, ValueError):
		return None


def _updated_key(item: dict) -> str:
	value = item.get("updated_at")
	return value if isinstance(value, str) else ""


def _candidates(data: Any) -> list[dict]:
	if not isinstance(data, list):
		return []
	out = []
	for item in data:
		if not isinstance(item, dict) or "pull_request" in item:
			continue
		if _positive(item.get("number")) is None:
			continue
		out.append(item)
	out.sort(key=_updated_key)
	return out


def cleanup_disabled(repo: str, candidates: list[dict], now: datetime, window_h: int, cap: int) -> int:
	"""Switch off: remove the label from candidates whose window has passed."""
	attempted = 0
	for item in candidates:
		if attempted >= cap:
			break
		ref = item.get("closed_at") if item.get("state") == "closed" and item.get("closed_at") else item.get("updated_at")
		stamp = parse_ts(ref)
		if stamp is None or now < stamp + timedelta(hours=window_h):
			continue
		attempted += 1
		number = item["number"]
		if _remove_label(repo, number):
			log("cleaned", "disabled_window_passed", number)
		else:
			log("error", "label_write", number)
	return attempted


def _budget_owner(number: int) -> bool:
	if not _flag("PERSIST_BUDGET_ENABLED"):
		return False
	if (Path(__file__).resolve().parent / "persist_budget.py").is_file():
		return True
	log("deferred", "budget_absent", number)
	return False


def check_one(repo: str, item: dict, login: str, runs_cache: Any, now: datetime, window_h: int) -> bool:
	"""Grade one candidate. True when the poller should re-trigger planning."""
	number = item["number"]
	labels = _str_labels(item.get("labels"))
	body = item.get("body") if isinstance(item.get("body"), str) else ""
	if ORCHESTRATOR_METADATA in body:
		if _remove_label(repo, number):
			log("skipped", "orchestrator_managed", number)
		else:
			log("error", "label_write", number)
		return False
	if _flag("WORKFLOW_HEAL_VERIFY_ENABLED") and HEAL_LABEL in labels:
		if _remove_label(repo, number):
			log("skipped", "heal_owned", number)
		else:
			log("error", "label_write", number)
		return False
	comments = _load_comments(repo, number)
	if comments is None:
		log("error", "gh_read", number)
		return False
	markers = parse_markers(comments, login)
	marker = active_marker(markers)
	if marker is None:
		if _remove_label(repo, number):
			log("skipped", "marker_absent", number)
		else:
			log("error", "label_write", number)
		return False
	run = select_run(runs_cache, marker)
	# The per-tick cache keeps only the 20 newest completed runs repo-wide, so
	# a cached run on a later commit may hide an evicted run on the merge
	# commit: read the workflow's own runs unless the cache hit is that run.
	if run is None or run["head_sha"] != marker["merge_sha"]:
		basename = marker["workflow"].rsplit("/", 1)[-1]
		data = _gh_json([
			f"repos/{repo}/actions/workflows/{basename}/runs?branch={quote(marker['branch'], safe='')}"
			f"&status=completed&created={quote('>=' + marker['merged_at'], safe='')}&per_page=100"])
		if not isinstance(data, dict):
			log("error", "gh_read", number)
			return False
		run = select_run(data, marker) or run
	contained = False
	if run is not None:
		if run["head_sha"] == marker["merge_sha"]:
			contained = True
		else:
			data = _gh_json([f"repos/{repo}/compare/{marker['merge_sha']}...{run['head_sha']}"])
			if not isinstance(data, dict):
				log("error", "gh_read", number)
				return False
			contained = data.get("status") in ("ahead", "identical")
	verdict: str | None = None
	failing_job = ""
	if run is not None and contained and marker.get("job"):
		data = _gh_json([f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100"])
		if not isinstance(data, dict):
			log("error", "gh_read", number)
			return False
		verdict, failing_job = job_verdict(data, marker["job"])
	result = decide(marker, run, contained, verdict, now, window_h)
	if result == "wait":
		log("pending", "no_run" if run is None else ("not_contained" if not contained else "run_inconclusive"), number)
		return False
	if result == "held":
		if not _remove_label(repo, number):
			log("error", "label_write", number)
			return False
		_journal(repo, number, "held", comments, login, f"check after merge held: run {run['id']}")
		log("held", "run_succeeded", number)
		return False
	if result == "unverified":
		if not _remove_label(repo, number):
			log("error", "label_write", number)
			return False
		if not _post_comment(repo, number, unverified_comment(marker, window_h)):
			log("error", "comment_write", number)
		_journal(repo, number, "unverified", comments, login, f"check after merge unverified after {window_h} h")
		log("unverified", "window_passed", number)
		return False
	# recurred
	budget_owner = _budget_owner(number)
	count = replan_count(markers)
	escalate = not budget_owner and count >= max_approaches()
	target = NEEDS_HUMAN_LABEL if escalate else PLANNING_LABEL
	patch = [f"repos/{repo}/issues/{number}", "-X", "PATCH", "-f", "state=open"]
	for label in labels_after_reopen(labels, target):
		patch += ["-f", f"labels[]={label}"]
	if _gh(patch) is None:
		log("error", "reopen_write", number)
		return False
	if budget_owner:
		next_step = "the persistence budget (P6) decides whether to re-plan."
	elif escalate:
		next_step = f"escalated to a person after {count} re-plans (`{NEEDS_HUMAN_LABEL}`)."
	else:
		next_step = f"the issue is reopened in `{PLANNING_LABEL}` and the plan is re-generated."
	if not _post_comment(repo, number, failed_comment(marker, run, failing_job, next_step)):
		# replan_count() counts trusted recurred markers; re-planning without one
		# would let the PERSIST_MAX_APPROACHES cap be exceeded. The issue is open
		# in its target phase, where standalone stall recovery picks it up.
		log("error", "comment_write", number)
		return False
	step = failing_job or marker.get("job") or "run"
	text = f"::error::post-merge {marker['workflow']} {step} concluded {_token(run.get('conclusion'))}\n"
	_journal(repo, number, "fail", comments, login, f"check after merge failed: run {run['id']}",
		failure=(marker["workflow"], step, text))
	if budget_owner:
		log("recurred", "budget_owner", number)
		return False
	if escalate:
		log("recurred", "escalated", number)
		return False
	log("recurred", "replan", number)
	return True


def rotate_window(candidates: list[dict], cap: int, run_number: Any) -> list[dict]:
	"""At most `cap` candidates, starting at (run_number * cap) mod len, so a
	waiting candidate (which writes nothing and keeps its updated_at) cannot
	hold every slot: each candidate is graded at least once every
	ceil(len / cap) ticks."""
	if not candidates or cap <= 0:
		return []
	text = str(run_number or "")
	start = (int(text) * cap) % len(candidates) if text.isdigit() else 0
	ordered = candidates[start:] + candidates[:start]
	return ordered[:cap]


def cmd_check(args: argparse.Namespace) -> None:
	out: dict = {"replan": [], "processed": 0}
	_write_out(args.out, out)
	if not REPO_RE.match(args.repo or ""):
		log("error", "invalid_repo")
		return
	data = _gh_json([f"repos/{args.repo}/issues?labels={LABEL_ENC}&state=all&per_page=50"])
	if not isinstance(data, list):
		log("error", "gh_read")
		return
	candidates = _candidates(data)
	now = _now()
	window_h = window_hours()
	cap = max_per_tick()
	if not enabled():
		cleanup_disabled(args.repo, candidates, now, window_h, cap)
		return
	if not candidates:
		return
	login = attempt_journal.resolve_login(args.trusted_login)
	if not login:
		log("error", "identity_unavailable")
		return
	runs_cache = _load_runs_file(args.runs_file)
	for item in rotate_window(candidates, cap, os.environ.get("GITHUB_RUN_NUMBER", "")):
		out["processed"] += 1
		try:
			if check_one(args.repo, item, login, runs_cache, now, window_h):
				out["replan"].append(item["number"])
		except Exception as exc:  # noqa: BLE001 - fail open per candidate
			log("error", "exception_" + type(exc).__name__, item.get("number"))
	_write_out(args.out, out)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Check after merge (AI_POSTMERGE_VERIFY_V1).")
	sub = parser.add_subparsers(dest="cmd", required=True)
	arm = sub.add_parser("arm")
	arm.add_argument("--repo", required=True)
	arm.add_argument("--issue", required=True)
	arm.add_argument("--pr", required=True)
	arm.add_argument("--merge-sha", default="")
	arm.add_argument("--merged-at", default="")
	arm.add_argument("--base", default="")
	arm.add_argument("--pr-labels-json", default="[]")
	arm.add_argument("--trusted-login")
	check = sub.add_parser("check")
	check.add_argument("--repo", required=True)
	check.add_argument("--out", required=True)
	check.add_argument("--runs-file")
	check.add_argument("--trusted-login")
	return parser


def main(argv: list[str] | None = None) -> int:
	try:
		args = build_parser().parse_args(argv)
	except SystemExit:
		log("error", "usage")
		return 0
	if attempt_journal is None:
		if args.cmd == "check":
			_write_out(args.out, {"replan": [], "processed": 0})
		log("error", "attempt_journal_unavailable")
		return 0
	try:
		{"arm": cmd_arm, "check": cmd_check}[args.cmd](args)
	except Exception as exc:  # noqa: BLE001 - fail open, never block a tick
		log("error", "exception_" + type(exc).__name__)
	return 0


if __name__ == "__main__":
	sys.exit(main())
