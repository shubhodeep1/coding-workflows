#!/usr/bin/env python3
"""Collect PR check-run context for review_autofix."""

from __future__ import annotations

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
FAILURE_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled", "stale"}


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
		return []
	try:
		obj = json.loads(text)
	except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
		return []
	pages = obj if isinstance(obj, list) else [obj]
	return [page for page in pages if isinstance(page, dict)]


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
		if status not in ("queued", "in_progress"):
			continue
		details_url = "" if run.get("details_url") is None else str(run.get("details_url"))
		if self_run_id and f"/actions/runs/{self_run_id}/job/" in details_url:
			continue
		wait_view.append({
			"id": _coerce_int(run.get("id"), 0),
			"status": status,
		})
	return sorted(wait_view, key=lambda item: (item["id"], item["status"]))


def _short(value: Any, limit: int = 400) -> str:
	text = "" if value is None else str(value)
	text = text.replace("\r", " ").replace("\n", " | ")
	if len(text) <= limit:
		return text
	return text[:limit] + "..."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, newurl):
		return None


_LOG_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


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
		start = max(0, content_length - log_tail_bytes)
		tail_req = urllib.request.Request(
			blob_url,
			headers={"Range": f"bytes={start}-{content_length - 1}"},
			method="GET",
		)
		with urllib.request.urlopen(tail_req, timeout=30) as resp:
			status = resp.getcode()
			if status == 206 or (status == 200 and content_length <= log_tail_bytes):
				out = resp.read()
			else:
				return ""
	except (OSError, ValueError, urllib.error.HTTPError, http.client.HTTPException):
		return ""
	if len(out) > log_tail_bytes:
		out = out[-log_tail_bytes:]
	lines = out.decode("utf-8", errors="replace").splitlines()
	if len(lines) > LOG_TAIL_LINES:
		lines = lines[-LOG_TAIL_LINES:]
	return "\n".join(lines)


def _build_context_text(*, raw_text: str, head_sha: str, final_status: str) -> str:
	runs = _extract_runs(raw_text)
	incomplete = [run for run in runs if run.get("status") in ("queued", "in_progress")]
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
	if final_status == "api_error":
		buf.write("Check-run API call failed; treat absence of failures as unknown rather than confirmed-passing.\n\n")
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

		head_sha = _load_head_sha(payload_path)
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

		while True:
			proc = _run_check_runs_api(repository=repository, head_sha=head_sha, script_dir=Path(__file__).resolve().parent)
			raw_text = proc.stdout or ""
			if proc.returncode != 0:
				if proc.stderr:
					sys.stderr.write(proc.stderr)
				final_status = "api_error"
				break

			wait_view = _build_wait_view(raw_text, self_run_id)
			in_flight = len(wait_view)
			if last_wait_view is not None and wait_view == last_wait_view:
				unchanged_wait_snapshots += 1
			else:
				last_wait_view = wait_view
				unchanged_wait_snapshots = 0

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
