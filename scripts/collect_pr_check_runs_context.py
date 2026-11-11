#!/usr/bin/env python3
"""Collect PR check-run context for review_autofix."""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


DEFAULT_WAIT_TIMEOUT_SECS = 300
MAX_WAIT_TIMEOUT_SECS = 3600
DEFAULT_POLL_INTERVAL_SECS = 20
MIN_POLL_INTERVAL_SECS = 5
MAX_POLL_INTERVAL_SECS = 300
BACKOFF_CAP_SECS = 120
DEFAULT_LOG_TAIL_BYTES = 16384
MAX_LOG_TAIL_BYTES = 131072
LOG_TAIL_LINES = 200
# Issue #8799: the ranged GET reads up to this many bytes from the end of the
# job log so the step's "##[error]" line is found even when a long post-job
# section (service-container teardown dumps) follows it. Not an env var: the
# window itself stays bounded by CHECK_RUNS_LOG_TAIL_BYTES and LOG_TAIL_LINES.
LOG_SCAN_BYTES = 262144
# Lines longer than this are cut before any byte budget is applied, so one
# huge line (an error embedding initcode hex) cannot crowd out the rest.
LOG_LINE_CHAR_CAP = 2000
LOG_TAIL_STEP_ERROR_MARKER = "##[error]"
LOG_TAIL_STEP_ERROR_AFTER_LINES = 5
# Issue #9326: the runner's own step-failure line. The runner writes it once
# for each `run:` step that exits non-zero; a passing step never writes it, so
# "##[error]" lines a passing test prints (e.g. lint self-tests) cannot outrank
# it. Only this one pattern is matched, and only when "##[error]" starts the
# message (optional BOM and runner timestamp first). Step-level messages such
# as "##[error]merge-train step shards failed: rest" sit just before it and
# land in the lead context without a pattern of their own.
LOG_TAIL_STEP_FAILURE_LINE_RE = re.compile(
	r"^\ufeff?(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z )?##\[error\]Process completed with exit code -?\d+"
)
FAILURE_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled", "stale", "startup_failure"}
# Full commit SHA accepted for PR_CHECK_RUNS_HEAD_SHA_OVERRIDE (#6918).
_HEAD_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# Cap on check identities listed per CHECK_RUNS_WAIT_PENDING line.
WAIT_PENDING_MAX_ENTRIES = 20


def _required_env(name: str) -> str:
	value = os.environ.get(name, "")
	if value:
		return value
	echo = f"::error::collect_pr_check_runs_context: required env {name} is unset."
	print(echo, file=sys.stderr)
	raise SystemExit(1)


def _write_text(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp_path: Path | None = None
	try:
		with NamedTemporaryFile(
			mode="w",
			encoding="utf-8",
			dir=str(path.parent),
			prefix=f".{path.name}.",
			suffix=".tmp",
			delete=False,
		) as handle:
			tmp_path = Path(handle.name)
			handle.write(text)
		tmp_path.replace(path)
	except Exception:
		if tmp_path is not None:
			try:
				tmp_path.unlink(missing_ok=True)
			except OSError:
				pass
		raise


def _sentinel_text(*, head_sha: str, collection_status: str, message: str) -> str:
	buf = io.StringIO()
	buf.write("PR_CHECK_RUNS_CONTEXT\n")
	buf.write(f"head_sha: {head_sha}\n")
	buf.write(f"collection_status: {collection_status}\n")
	buf.write("total_check_runs: 0\n")
	buf.write("failed_count: 0\n")
	buf.write("incomplete_count: 0\n")
	buf.write("\n")
	buf.write(f"{message}\n")
	return buf.getvalue()


def _clamp_wait_timeout(raw: str) -> int:
	if not raw.isdigit():
		return DEFAULT_WAIT_TIMEOUT_SECS
	value = int(raw)
	if value > MAX_WAIT_TIMEOUT_SECS:
		return DEFAULT_WAIT_TIMEOUT_SECS
	return value


def _clamp_poll_interval(raw: str) -> int:
	if not raw.isdigit():
		return DEFAULT_POLL_INTERVAL_SECS
	value = int(raw)
	if value < MIN_POLL_INTERVAL_SECS or value > MAX_POLL_INTERVAL_SECS:
		return DEFAULT_POLL_INTERVAL_SECS
	return value


def _clamp_log_tail_bytes(raw: str) -> int:
	try:
		value = int(raw)
	except (TypeError, ValueError):
		return DEFAULT_LOG_TAIL_BYTES
	if value < 0:
		return 0
	if value > MAX_LOG_TAIL_BYTES:
		return MAX_LOG_TAIL_BYTES
	return value


def _load_head_sha(payload_path: Path) -> str:
	try:
		payload = json.loads(payload_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
		return ""
	if not isinstance(payload, dict):
		return ""
	head = payload.get("head") or {}
	if not isinstance(head, dict):
		return ""
	head_sha = head.get("sha")
	if head_sha in (None, "", "null"):
		return ""
	return str(head_sha)


def _resolve_head_sha(payload_path: Path) -> tuple[str, str]:
	"""Return (head_sha, error) for the check-run query.

	Check-failure triage sets PR_CHECK_RUNS_HEAD_SHA_OVERRIDE to the failing
	check's SHA so a PR head that advanced after the failure cannot swap a
	later commit's check runs into the diagnosis (#6918). An invalid override
	fails closed and never falls back to the PR payload head. Unset or empty
	keeps the review/autofix behaviour: the PR payload's current head.
	"""
	override = os.environ.get("PR_CHECK_RUNS_HEAD_SHA_OVERRIDE", "")
	if override:
		if _HEAD_SHA_RE.fullmatch(override):
			return override.lower(), ""
		return "", "invalid_override"
	return _load_head_sha(payload_path), ""


def _run_check_runs_api(*, repository: str, head_sha: str, script_dir: Path) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env.update({
		"GH_HELPERS_PATH": str(script_dir / "gh_helpers.sh"),
		"REPOSITORY": repository,
		"HEAD_SHA": head_sha,
	})
	shell_script = """set -euo pipefail
source "${GH_HELPERS_PATH}" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
gh_retry gh api --paginate --slurp "repos/${REPOSITORY}/commits/${HEAD_SHA}/check-runs?per_page=100"
"""
	return subprocess.run(
		["bash", "-c", shell_script],
		check=False,
		capture_output=True,
		text=True,
		encoding="utf-8",
		errors="replace",
		env=env,
	)


def _parse_pages(raw_text: str) -> list[dict[str, Any]]:
	text = raw_text.strip()
	if not text:
		raise ValueError("empty_output")
	try:
		obj = json.loads(text)
	except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
		raise ValueError("invalid_json") from None
	pages = obj if isinstance(obj, list) else [obj]
	if not pages:
		raise ValueError("no_pages")
	if any(not isinstance(page, dict) for page in pages):
		raise ValueError("invalid_page")
	if any(not isinstance(page.get("check_runs"), list) for page in pages):
		raise ValueError("missing_check_runs")
	return pages


def _extract_runs(raw_text: str) -> list[dict[str, Any]]:
	runs: list[dict[str, Any]] = []
	for page in _parse_pages(raw_text):
		page_runs = page.get("check_runs", [])
		if isinstance(page_runs, list):
			runs.extend(run for run in page_runs if isinstance(run, dict))
	return runs


def _coerce_int(value: Any, default: int = 0) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


def _build_wait_view(raw_text: str, self_run_id: str) -> list[dict[str, Any]]:
	wait_view: list[dict[str, Any]] = []
	for run in _extract_runs(raw_text):
		status = "" if run.get("status") is None else str(run.get("status"))
		# GitHub also returns requested/pending/waiting. A completed-only
		# snapshot is required before a clean Claude-fixer review may merge.
		if status == "completed":
			continue
		details_url = "" if run.get("details_url") is None else str(run.get("details_url"))
		if self_run_id and f"/actions/runs/{self_run_id}/job/" in details_url:
			continue
		wait_view.append({
			"id": _coerce_int(run.get("id"), 0),
			"status": status,
		})
	return sorted(wait_view, key=lambda item: (item["id"], item["status"]))


def _build_wait_identity(raw_text: str, self_run_id: str) -> tuple[list[dict[str, Any]], int]:
	"""Return (identities, self_excluded) for diagnostics only.

	Uses the same skip rules as _build_wait_view, so the identity list
	describes exactly the in-flight set. It is never compared, so names or
	started_at cannot affect the backoff. details_url is only substring
	matched for the self-run rule and is never returned; output/summary and
	html_url are never read.
	"""
	identities: list[dict[str, Any]] = []
	self_excluded = 0
	for run in _extract_runs(raw_text):
		status = "" if run.get("status") is None else str(run.get("status"))
		if status == "completed":
			continue
		details_url = "" if run.get("details_url") is None else str(run.get("details_url"))
		if self_run_id and f"/actions/runs/{self_run_id}/job/" in details_url:
			self_excluded += 1
			continue
		app = run.get("app") or {}
		if not isinstance(app, dict):
			app = {}
		identities.append({
			"id": _coerce_int(run.get("id"), 0),
			"name": run.get("name"),
			"app_slug": app.get("slug"),
			"status": status,
			"started_at": run.get("started_at"),
		})
	return identities, self_excluded


def _short(value: Any, limit: int = 400) -> str:
	text = "" if value is None else str(value)
	text = text.replace("\r", " ").replace("\n", " | ")
	if len(text) <= limit:
		return text
	return text[:limit] + "..."


def _sanitize_wait_field(value: Any, limit: int = 80) -> str:
	"""Render untrusted check-run text as one safe log token."""
	text = "" if value is None else str(value)
	# CR/LF go first so a later step cannot join ":\n:" into "::".
	text = text.replace("\r", "").replace("\n", "")
	text = "".join("_" if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in text)
	while "::" in text:
		text = text.replace("::", ":_")
	# Legacy "##[command]" annotation syntax.
	while "##[" in text:
		text = text.replace("##[", "#_[")
	text = re.sub(r"[\s|,]", "_", text)
	if not text:
		text = "-"
	return _short(text, limit)


def _format_wait_elapsed(started_at: Any, now: float) -> str:
	if started_at in (None, ""):
		return "-"
	try:
		ts = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
	except (TypeError, ValueError):
		return "-"
	if ts.tzinfo is None or ts.utcoffset() is None:
		return "-"
	try:
		return str(max(0, int(now - ts.timestamp())))
	except (OverflowError, ValueError, OSError):
		return "-"


def _format_wait_pending_line(
	*,
	head_sha: str,
	poll: int,
	in_flight: int,
	self_excluded: int,
	entries: list[dict[str, Any]],
	now: float,
) -> str:
	ordered = sorted(entries, key=lambda item: _coerce_int(item.get("id"), 0))
	shown = ordered[:WAIT_PENDING_MAX_ENTRIES]
	rendered = [
		"|".join((
			_sanitize_wait_field(item.get("name")),
			_sanitize_wait_field(item.get("app_slug")),
			_sanitize_wait_field(item.get("status")),
			_format_wait_elapsed(item.get("started_at"), now),
		))
		for item in shown
	]
	checks = ",".join(rendered) if rendered else "-"
	line = (
		f"CHECK_RUNS_WAIT_PENDING head_sha={_sanitize_wait_field(head_sha, 40)}"
		f" poll={int(poll)} in_flight={int(in_flight)} self_excluded={int(self_excluded)}"
		f" checks={checks}"
	)
	if len(ordered) > WAIT_PENDING_MAX_ENTRIES:
		line += f" truncated={len(ordered) - WAIT_PENDING_MAX_ENTRIES}"
	return line


class _NoRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, newurl):
		return None


_LOG_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _cap_long_lines(lines: list[str], limit: int = LOG_LINE_CHAR_CAP) -> list[str]:
	"""Return a new list with every line over ``limit`` characters cut."""
	capped: list[str] = []
	for line in lines:
		if len(line) > limit:
			capped.append(line[:limit] + f" [\u2026 {len(line) - limit} chars truncated]")
		else:
			capped.append(line)
	return capped


def _utf8_len(text: str) -> int:
	return len(text.encode("utf-8"))


# Credential shapes masked on the full decoded scan before any line is cut, so
# a cut can never leave a partial token (issue #8799). A local copy of the
# simple patterns in workflow_failure_heal._SECRET_PATTERNS: this helper is
# staged on its own and cannot import that module.
_LOG_TAIL_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
	# Scheme bounded to 32 chars (unlike heal) so long hex runs cannot backtrack quadratically.
	(re.compile(r"([A-Za-z][A-Za-z0-9+.-]{0,31}://)[^/@\s]+@"), r"\1[redacted]@"),
	# Separators use [ \t] (unlike heal): this runs on the whole multi-line scan,
	# and a \s match must not swallow the next line's "##[error]" anchor.
	(re.compile(r"(authorization[ \t]*:[ \t]*)(?:(?:bearer|basic|token)[ \t]+)?\S+", re.IGNORECASE), r"\1[redacted]"),
	(re.compile(r"(extraheader[ \t]*[=:][ \t]*)\S+(?:[ \t]+\S+)?", re.IGNORECASE), r"\1[redacted]"),
	(re.compile(r"(basic[ \t]+)[A-Za-z0-9+/=_-]{8,}", re.IGNORECASE), r"\1[redacted]"),
	(re.compile(r"([Bb]earer[ \t]+)\S+"), r"\1[redacted]"),
	(re.compile(r"(github_pat_|gh[pousr]_)[A-Za-z0-9_]+"), r"\1[redacted]"),
	(re.compile(r"(sk-(?:or|ant)-)[A-Za-z0-9_-]+"), r"\1[redacted]"),
)


def _redact_log_encoded_credential(match: re.Match[str]) -> str:
	"""Local copy of workflow_failure_heal._redact_encoded_credential."""
	value = match.group()
	for offset in range(4):
		try:
			decoded = base64.b64decode(value[offset:] + "=" * (-len(value[offset:]) % 4), altchars=b"-_", validate=True)
		except (ValueError, base64.binascii.Error):
			continue
		if b"x-access-token:" in decoded.lower() or (all(0x20 <= byte < 0x7F for byte in decoded) and re.search(rb"[^:\s]:\S{36,}", decoded)):
			return "[redacted]"
	return value


def _redact_log_text(text: str) -> str:
	for pattern, replacement in _LOG_TAIL_SECRET_PATTERNS:
		text = pattern.sub(replacement, text)
	# Encoded credentials need not follow a Basic header (heal.redact_secrets).
	return re.sub(r"[A-Za-z0-9+/_-]{24,}={0,2}", _redact_log_encoded_credential, text)


def _select_log_tail_window(out: bytes, *, log_tail_bytes: int, partial_start: bool) -> str:
	"""Pick the ``failed[i].log_tail`` text from the scanned end of a job log.

	When the scan holds a step-level ``##[error]`` line, the window ends at the
	first ``##[error]Process completed with exit code N`` line
	(``LOG_TAIL_STEP_FAILURE_LINE_RE``) when there is one, otherwise at the
	first ``##[error]`` line (issue #9326). It keeps up to 5 following lines
	and is trimmed from the front to ``log_tail_bytes`` bytes and
	``LOG_TAIL_LINES`` lines (issue #8799).
	Otherwise the window is the pre-#8799 one: the last ``log_tail_bytes``
	bytes, last ``LOG_TAIL_LINES`` lines. A ``log_tail_anchor:`` line names the
	window kind; it is informational and not counted in the byte budget.
	"""
	if log_tail_bytes <= 0 or not out:
		return ""
	decoded = _redact_log_text(out.decode("utf-8", errors="replace"))
	out = decoded.encode("utf-8")
	lines = decoded.splitlines()
	first_marker_index: int | None = None
	step_failure_index: int | None = None
	for index, line in enumerate(lines):
		# A partial range may start mid-line; that fragment is never an anchor.
		if index == 0 and partial_start:
			continue
		if LOG_TAIL_STEP_ERROR_MARKER not in line:
			continue
		if first_marker_index is None:
			first_marker_index = index
		if LOG_TAIL_STEP_FAILURE_LINE_RE.match(line):
			step_failure_index = index
			break
	# Issue #9326: prefer the runner's step-failure line; otherwise keep the
	# pre-#9326 anchor, the first "##[error]" line.
	anchor = step_failure_index if step_failure_index is not None else first_marker_index
	if anchor is None:
		tail = out[-log_tail_bytes:] if len(out) > log_tail_bytes else out
		tail_lines = tail.decode("utf-8", errors="replace").splitlines()
		if len(tail_lines) > LOG_TAIL_LINES:
			tail_lines = tail_lines[-LOG_TAIL_LINES:]
		if any(len(line) > LOG_LINE_CHAR_CAP for line in tail_lines):
			# A long line would crowd out the rest: cap every line first, then
			# fill the byte and line budget from the end with whole lines.
			pool = _cap_long_lines(lines[1:] if partial_start else lines)
			kept: list[str] = []
			used = 0
			for line in reversed(pool):
				cost = _utf8_len(line) + (1 if kept else 0)
				if len(kept) >= LOG_TAIL_LINES or used + cost > log_tail_bytes:
					break
				kept.append(line)
				used += cost
			if not kept and pool:
				kept = [pool[-1].encode("utf-8")[-log_tail_bytes:].decode("utf-8", errors="ignore")]
			kept.reverse()
			tail_lines = kept
		body = "\n".join(tail_lines)
		if not body:
			return ""
		return "log_tail_anchor: end_of_log\n" + body
	capped = _cap_long_lines(lines[: anchor + 1 + LOG_TAIL_STEP_ERROR_AFTER_LINES])
	# The partial first line of a ranged scan is never kept as lead context.
	before = capped[1 if partial_start else 0:anchor]
	error_line = capped[anchor]
	after = capped[anchor + 1:]
	# Trailing after-lines go first; the error line itself always survives.
	while after and (
		_utf8_len("\n".join([error_line, *after])) > log_tail_bytes
		or 1 + len(after) > LOG_TAIL_LINES
	):
		after.pop()
	if _utf8_len(error_line) > log_tail_bytes:
		# Keep the line end, where the "##[error]" text and message sit.
		window = [error_line.encode("utf-8")[-log_tail_bytes:].decode("utf-8", errors="ignore")]
	else:
		window = [error_line, *after]
		used = _utf8_len("\n".join(window))
		lead: list[str] = []
		for line in reversed(before):
			if len(window) + len(lead) + 1 > LOG_TAIL_LINES:
				break
			cost = _utf8_len(line) + 1
			if used + cost > log_tail_bytes:
				break
			lead.append(line)
			used += cost
		lead.reverse()
		window = lead + window
	body = "\n".join(window)
	if not body:
		return ""
	return "log_tail_anchor: step_error\n" + body


def _fetch_log_tail(*, details_url: str, log_tail_bytes: int, repository: str, token: str) -> str:
	if log_tail_bytes <= 0:
		return ""
	if not details_url or not repository or not token:
		return ""
	match = re.search(r"/actions/runs/(\d+)/job/(\d+)", details_url)
	if not match:
		return ""
	job_id = match.group(2)
	try:
		api_req = urllib.request.Request(
			f"https://api.github.com/repos/{repository}/actions/jobs/{job_id}/logs",
			headers={
				"Authorization": f"Bearer {token}",
				"Accept": "application/vnd.github+json",
				"User-Agent": "coding-workflows-review-autofix",
				"X-GitHub-Api-Version": "2022-11-28",
			},
			method="GET",
		)
		try:
			with _LOG_REDIRECT_OPENER.open(api_req, timeout=10):
				return ""
		except urllib.error.HTTPError as exc:
			try:
				if exc.code not in (301, 302, 303, 307, 308):
					return ""
				blob_url = exc.headers.get("Location") or ""
			finally:
				exc.close()
		if not blob_url:
			return ""
		head_req = urllib.request.Request(blob_url, method="HEAD")
		with urllib.request.urlopen(head_req, timeout=10) as resp:
			content_length = int(resp.headers.get("Content-Length", "0"))
		if content_length <= 0:
			return ""
		# Issue #8799: scan up to LOG_SCAN_BYTES so the step error is found
		# behind a long post-job section. Still exactly one ranged GET.
		scan_bytes = max(log_tail_bytes, min(content_length, LOG_SCAN_BYTES))
		start = max(0, content_length - scan_bytes)
		# Read one extra byte before the scan when it starts past byte 0: a
		# newline there means the first scanned line is complete, not a
		# fragment, so it may still be the step-error anchor.
		probe = 1 if start > 0 else 0
		tail_req = urllib.request.Request(
			blob_url,
			headers={"Range": f"bytes={start - probe}-{content_length - 1}"},
			method="GET",
		)
		with urllib.request.urlopen(tail_req, timeout=30) as resp:
			status = resp.getcode()
			if status == 206 or (status == 200 and content_length <= scan_bytes):
				out = resp.read(scan_bytes + probe + 1)
			else:
				return ""
	except (OSError, ValueError, urllib.error.HTTPError, http.client.HTTPException):
		return ""
	partial_start = start > 0
	if probe and len(out) == scan_bytes + probe:
		partial_start = out[:1] != b"\n"
		out = out[1:]
	if len(out) > scan_bytes:
		out = out[-scan_bytes:]
	return _select_log_tail_window(out, log_tail_bytes=log_tail_bytes, partial_start=partial_start)


def _build_context_text(*, raw_text: str, head_sha: str, final_status: str) -> str:
	if final_status == "api_error":
		return _sentinel_text(
			head_sha=head_sha,
			collection_status=final_status,
			message="Check-run API output was unavailable or invalid; treat absence of failures as unknown rather than confirmed-passing.",
		)
	runs = _extract_runs(raw_text)
	# Only the post-review clean-result probe opts in: its own running job
	# cannot complete before the probe, but every other incomplete check must
	# still block auto-merge. Ordinary reviewer context keeps the self entry.
	if os.environ.get("CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT", "false") == "true":
		self_run_id = os.environ.get("SELF_RUN_ID", "")
		if self_run_id.isdigit():
			runs = [run for run in runs if f"/actions/runs/{self_run_id}/job/" not in str(run.get("details_url") or "")]
	incomplete = [run for run in runs if run.get("status") != "completed" or run.get("conclusion") is None]
	failed = [
		run for run in runs
		if run.get("status") == "completed"
		and (run.get("conclusion") or "") in FAILURE_CONCLUSIONS
	]
	log_tail_bytes = _clamp_log_tail_bytes(os.environ.get("CHECK_RUNS_LOG_TAIL_BYTES", str(DEFAULT_LOG_TAIL_BYTES)))
	repository = os.environ.get("GITHUB_REPOSITORY", "")
	token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""

	buf = io.StringIO()
	buf.write("PR_CHECK_RUNS_CONTEXT\n")
	buf.write(f"head_sha: {head_sha}\n")
	buf.write(f"collection_status: {final_status}\n")
	buf.write(f"total_check_runs: {len(runs)}\n")
	buf.write(f"failed_count: {len(failed)}\n")
	buf.write(f"incomplete_count: {len(incomplete)}\n")
	buf.write("\n")
	if not failed and not incomplete and final_status not in ("api_error",):
		buf.write("No failed or incomplete check-runs detected on the PR head SHA.\n")
	for idx, run in enumerate(failed):
		app = run.get("app") or {}
		if not isinstance(app, dict):
			app = {}
		output = run.get("output") or {}
		if not isinstance(output, dict):
			output = {}
		buf.write(f"failed[{idx}].name: {_short(run.get('name'))}\n")
		buf.write(f"failed[{idx}].status: {_short(run.get('status'))}\n")
		buf.write(f"failed[{idx}].conclusion: {_short(run.get('conclusion'))}\n")
		buf.write(f"failed[{idx}].app: {_short(app.get('slug'))}\n")
		buf.write(f"failed[{idx}].html_url: {_short(run.get('html_url'))}\n")
		buf.write(f"failed[{idx}].details_url: {_short(run.get('details_url'))}\n")
		raw_summary = "" if output.get("summary") is None else str(output.get("summary"))
		summary = _short(raw_summary, 1200)
		buf.write(f"failed[{idx}].title: {_short(output.get('title'))}\n")
		buf.write(f"failed[{idx}].summary: {summary}\n")
		log_tail = ""
		if not raw_summary.strip():
			log_tail = _fetch_log_tail(
				details_url=str(run.get("details_url") or ""),
				log_tail_bytes=log_tail_bytes,
				repository=repository,
				token=token,
			)
		buf.write(f"failed[{idx}].log_tail ({len(log_tail)} chars):\n")
		if log_tail:
			buf.write(log_tail)
			if not log_tail.endswith("\n"):
				buf.write("\n")
		buf.write("\n")
	for idx, run in enumerate(incomplete):
		buf.write(f"incomplete[{idx}].name: {_short(run.get('name'))}\n")
		buf.write(f"incomplete[{idx}].status: {_short(run.get('status'))}\n")
		buf.write(f"incomplete[{idx}].html_url: {_short(run.get('html_url'))}\n")
		buf.write("\n")
	return buf.getvalue()


def _write_context_file(*, out_path: Path, raw_text: str, head_sha: str, final_status: str) -> bool:
	try:
		context_text = _build_context_text(raw_text=raw_text, head_sha=head_sha, final_status=final_status)
		_write_text(out_path, context_text)
		return out_path.exists() and out_path.stat().st_size > 0
	except Exception:
		traceback.print_exc(file=sys.stderr)
		return False


def _emit_final_diagnostics(out_path: Path) -> None:
	try:
		payload = out_path.read_bytes()
	except OSError:
		return
	print(f"Check-run context bytes: {len(payload)}")
	if payload:
		print(f"Check-run context sha256: {hashlib.sha256(payload).hexdigest()}")


def main() -> int:
	payload_path = Path(_required_env("PR_PAYLOAD_FILE"))
	out_path = Path(_required_env("PR_CHECK_RUNS_CONTEXT_FILE"))
	_write_text(out_path, "")
	head_sha = ""

	try:
		if os.environ.get("CHECK_RUNS_AUTOFIX_ENABLED", "true") == "false":
			_write_text(
				out_path,
				_sentinel_text(
					head_sha="",
					collection_status="disabled",
					message="Check-run autofix context collection is disabled (CHECK_RUNS_AUTOFIX_ENABLED=false).",
				),
			)
			print("CHECK_RUNS_AUTOFIX disabled via CHECK_RUNS_AUTOFIX_ENABLED=false")
			return 0

		head_sha, head_sha_error = _resolve_head_sha(payload_path)
		if head_sha_error == "invalid_override":
			_write_text(
				out_path,
				_sentinel_text(
					head_sha="",
					collection_status="unavailable",
					message="Supplied check head SHA is invalid; no check-run context collected.",
				),
			)
			print("::warning::CHECK_RUNS_AUTOFIX head SHA override invalid.")
			return 0
		if not head_sha:
			_write_text(
				out_path,
				_sentinel_text(
					head_sha="",
					collection_status="unavailable",
					message="Unable to determine PR head SHA; no check-run context available.",
				),
			)
			print("::warning::CHECK_RUNS_AUTOFIX head SHA missing from PR payload.")
			return 0

		wait_timeout = _clamp_wait_timeout(os.environ.get("CHECK_RUNS_WAIT_TIMEOUT_SECS", str(DEFAULT_WAIT_TIMEOUT_SECS)))
		poll_interval = _clamp_poll_interval(os.environ.get("CHECK_RUNS_POLL_INTERVAL_SECS", str(DEFAULT_POLL_INTERVAL_SECS)))
		backoff_cap = max(BACKOFF_CAP_SECS, poll_interval)
		repository = os.environ.get("GITHUB_REPOSITORY", "")
		self_run_id = os.environ.get("SELF_RUN_ID", "")
		deadline = int(time.time()) + wait_timeout
		last_wait_view: list[dict[str, Any]] | None = None
		unchanged_wait_snapshots = 0
		final_status = "ready"
		raw_text = ""
		wait_started = int(time.time())
		wait_polls = 0
		wait_last_in_flight = 0

		while True:
			wait_polls += 1
			proc = _run_check_runs_api(repository=repository, head_sha=head_sha, script_dir=Path(__file__).resolve().parent)
			raw_text = proc.stdout or ""
			if proc.returncode != 0:
				if proc.stderr:
					sys.stderr.write(proc.stderr)
				final_status = "api_error"
				break
			try:
				_parse_pages(raw_text)
			except ValueError as exc:
				print(f"::warning::CHECK_RUNS_AUTOFIX_MALFORMED_OUTPUT head_sha={_short(head_sha, 40)} reason={exc} bytes={len(raw_text.encode('utf-8'))}")
				now = int(time.time())
				if now >= deadline:
					final_status = "api_error"
					break
				time.sleep(min(poll_interval, deadline - now))
				if int(time.time()) >= deadline:
					final_status = "api_error"
					break
				continue

			wait_view = _build_wait_view(raw_text, self_run_id)
			in_flight = len(wait_view)
			wait_last_in_flight = in_flight
			if last_wait_view is not None and wait_view == last_wait_view:
				unchanged_wait_snapshots += 1
			else:
				last_wait_view = wait_view
				unchanged_wait_snapshots = 0

			if in_flight > 0:
				# Diagnostics only (fail-open): never touches final_status,
				# the wait view or the backoff counter.
				try:
					wait_entries, wait_self_excluded = _build_wait_identity(raw_text, self_run_id)
					print(_format_wait_pending_line(
						head_sha=head_sha,
						poll=wait_polls,
						in_flight=in_flight,
						self_excluded=wait_self_excluded,
						entries=wait_entries,
						now=time.time(),
					))
				except Exception as exc:
					sys.stderr.write(f"collect_pr_check_runs_context: wait diagnostics skipped ({type(exc).__name__})\n")

			if in_flight == 0:
				final_status = "ready"
				break

			now = int(time.time())
			if now >= deadline:
				final_status = "timeout"
				print(f"::warning::CHECK_RUNS_WAIT_TIMEOUT reached after {wait_timeout}s with {in_flight} check-run(s) still queued/in_progress; proceeding with snapshot.")
				break

			remaining = deadline - now
			sleep_secs = poll_interval
			if unchanged_wait_snapshots >= 2:
				sleep_secs = poll_interval * 4
			elif unchanged_wait_snapshots >= 1:
				sleep_secs = poll_interval * 2
			if sleep_secs > backoff_cap:
				sleep_secs = backoff_cap
			if sleep_secs > remaining:
				sleep_secs = remaining
			print(f"Waiting for {in_flight} in-progress/queued check-run(s) on {head_sha} (sleep {sleep_secs}s, deadline in {remaining}s)…")
			time.sleep(max(0, sleep_secs))

		try:
			print(
				f"CHECK_RUNS_WAIT_EXIT head_sha={_sanitize_wait_field(head_sha, 40)}"
				f" status={final_status} polls={wait_polls}"
				f" waited_s={max(0, int(time.time()) - wait_started)}"
				f" last_in_flight={wait_last_in_flight}"
			)
		except Exception as exc:
			sys.stderr.write(f"collect_pr_check_runs_context: wait diagnostics skipped ({type(exc).__name__})\n")

		writer_ok = _write_context_file(out_path=out_path, raw_text=raw_text, head_sha=head_sha, final_status=final_status)
		if not writer_ok:
			_write_text(
				out_path,
				_sentinel_text(
					head_sha=head_sha,
					collection_status="writer_error",
					message="Check-run snapshot writer failed; treat absence of failures as unknown rather than confirmed-passing.",
				),
			)
			print(f"::warning::CHECK_RUNS_AUTOFIX_WRITER_ERROR head_sha={head_sha} writer_ok={writer_ok}")

		_emit_final_diagnostics(out_path)
		return 0
	except Exception:
		traceback.print_exc(file=sys.stderr)
		try:
			_write_text(
				out_path,
				_sentinel_text(
					head_sha=head_sha,
					collection_status="writer_error",
					message="Check-run snapshot writer failed; treat absence of failures as unknown rather than confirmed-passing.",
				),
			)
		except Exception:
			traceback.print_exc(file=sys.stderr)
		print(f"::warning::CHECK_RUNS_AUTOFIX_WRITER_ERROR head_sha={head_sha} writer_ok=False")
		_emit_final_diagnostics(out_path)
		return 0


if __name__ == "__main__":
	raise SystemExit(main())
