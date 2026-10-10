#!/usr/bin/env python3
"""One-time follow-up for PR #6576's failed ``orchestrate-poll`` shard (issue #7072).

Single-use helper for ``.github/workflows/pr6576-shard-followup.yml`` (registry:
``docs/scripts-pending-removal.md``). The postmortem
``docs/postmortems/2026-10-10-pr-6576-orchestrate-poll-shard-1.md`` could not
fetch the failed job log and most of the partition could not run in the
implement sandbox, so the original failure was never identified. This helper:

- ``guard``        prints ``done=true|false``: true once the pipeline account
                   (the ``GH_PAT`` login) has posted the result marker on the
                   follow-up issue. Any read failure exits non-zero (fail closed).
- ``locate``       resolves PR #6576's recorded head, finds the failed
                   ``orchestrate-poll (<g>)`` CI jobs, downloads their logs
                   (redacted before disk) and extracts the failing test names
                   with the heal intake's parser. Writes ``evidence.json``.
- ``rerun``        no network: reruns the logged failing tests on current main,
                   or (when no log or no names were found) reproduces the CI
                   partition at the recorded head and then reruns its failures
                   on main. A name is ``reproduced_on_main`` only when it fails
                   in both of two main attempts. Writes ``results.json``.
- ``file-defect``  files one standalone issue for reproduced failures, deduped by
                   its body marker.
- ``report``       posts the single marker comment and writes the job summary.

Every log-derived string is neutralised before it reaches GitHub; test names
are used only after matching ``^test_[A-Za-z0-9_]{1,160}$``. Log prefix:
``PR6576_FOLLOWUP``. Assumption (documented per the approved plan): the CI
partition reproduced here is the one ``.github/workflows/ci.yml`` used for
#6576 (4 groups x CI_POLL_TEST_SHARDS=4 local shards, 1-based ``NR % total``).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

MARKER = os.environ.get("MARKER") or "<!-- ai:pr6576-shard-followup:v1 -->"
DEFECT_MARKER = os.environ.get("DEFECT_MARKER") or "<!-- ai:pr6576-shard-followup-defect:v1 -->"
DEFECT_SINCE = "2026-10-10T00:00:00Z"
POLL_MODULE = "tests/test_orchestrate_poll_process.py"
FAST_FAIL_PREFIX = "test_implementation_failed_"
GROUPS = 4
SHARDS = 4
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TEST_NAME_RE = re.compile(r"^test_[A-Za-z0-9_]{1,160}$")
JOB_NAME_RE = re.compile(r"^orchestrate-poll \([0-3]\)$")
MAX_RUN_PAGES = 3
MAX_JOB_CALLS = 8
MAX_LOGS = 4
EXCERPT_LINES = 40
EXCERPT_TOTAL_BYTES = 8000
HEAL_SCRIPT = Path(__file__).resolve().parent / "workflow_failure_heal.py"


class FollowupError(Exception):
	"""A failure that must stop the run before any GitHub write."""


def _log(outcome: str, reason: str = "", **fields: Any) -> None:
	parts = [f"PR6576_FOLLOWUP outcome={outcome}"]
	if reason:
		parts.append(f"reason={reason}")
	parts.extend(f"{key}={value}" for key, value in fields.items())
	print(" ".join(parts), file=sys.stderr, flush=True)


def _gh_json(args: list[str], paginate: bool = False, input_obj: Any = None) -> Any:
	"""Run ``gh api`` and parse JSON. Raises FollowupError on any failure."""
	cmd = ["gh", "api"]
	if paginate:
		cmd += ["--paginate", "--slurp"]
	if input_obj is not None:
		cmd += ["--input", "-"]
	cmd += args
	proc = subprocess.run(cmd, input=json.dumps(input_obj) if input_obj is not None else None, capture_output=True, text=True, check=False)
	if proc.returncode != 0:
		raise FollowupError(f"gh api failed (exit {proc.returncode})")
	try:
		data = json.loads(proc.stdout or "null")
	except json.JSONDecodeError as exc:
		raise FollowupError("gh api returned invalid JSON") from exc
	if paginate:
		if not isinstance(data, list):
			raise FollowupError("paginated response is not a list of pages")
		flat: list[Any] = []
		for page in data:
			if isinstance(page, list):
				flat.extend(page)
			else:
				raise FollowupError("paginated page is not a list")
		return flat
	return data


def _gh_raw(args: list[str]) -> tuple[int, bytes, str]:
	proc = subprocess.run(["gh", "api", *args], capture_output=True, check=False)
	return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace")


def _write_output(**values: Any) -> None:
	path = os.environ.get("GITHUB_OUTPUT")
	lines = [f"{key}={value}" for key, value in values.items()]
	for line in lines:
		print(line)
	if path:
		with open(path, "a", encoding="utf-8") as handle:
			handle.write("\n".join(lines) + "\n")


def _login() -> str:
	user = _gh_json(["user"])
	login = user.get("login") if isinstance(user, dict) else None
	if not isinstance(login, str) or not login:
		raise FollowupError("pipeline login unavailable")
	return login


def _neutralize(text: Any, limit: int = 2000) -> str:
	"""Make untrusted log text safe to embed in a GitHub comment or issue."""
	value = str(text if text is not None else "")
	value = value.replace("\r", "")
	value = value.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
	value = re.sub(r"`{3,}", "'''", value)
	value = "\n".join(re.sub(r"^(\s*)::", r"\1: :", line) for line in value.split("\n"))
	if len(value) > limit:
		value = value[:limit] + "... [truncated]"
	return value


def _safe_names(names: Any) -> list[str]:
	out: list[str] = []
	for name in names or []:
		if isinstance(name, str) and TEST_NAME_RE.match(name) and name not in out:
			out.append(name)
	return out


# --------------------------------------------------------------------- guard

def cmd_guard(args: argparse.Namespace) -> int:
	try:
		login = _login()
		comments = _gh_json([f"repos/{args.repo}/issues/{args.issue}/comments?per_page=100"], paginate=True)
	except FollowupError as exc:
		_log("failed", "guard_unavailable", detail=str(exc).replace(" ", "_"))
		return 1
	done = any(
		isinstance(c, dict)
		and (c.get("user") or {}).get("login") == login
		and MARKER in (c.get("body") or "")
		for c in comments
	)
	if done:
		_log("skip", "marker_present", issue=args.issue)
	_write_output(done="true" if done else "false")
	return 0


# -------------------------------------------------------------------- locate

def _failing_tests_from_logs(log_files: list[Path]) -> list[str]:
	if not log_files:
		return []
	proc = subprocess.run(
		[sys.executable, str(HEAL_SCRIPT), "heal-scope", "failing-tests", "--log-files", *[str(p) for p in log_files]],
		capture_output=True, text=True, check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
	)
	if proc.returncode != 0:
		return []
	try:
		data = json.loads(proc.stdout or "{}")
	except json.JSONDecodeError:
		return []
	return _safe_names(data.get("names") if isinstance(data, dict) else [])


def _redact(raw: bytes) -> bytes:
	proc = subprocess.run([sys.executable, str(HEAL_SCRIPT), "redact-stream"], input=raw, capture_output=True, check=False)
	if proc.returncode != 0:
		raise FollowupError("redact-stream failed")
	return proc.stdout


def locate(repo: str, pr: str, branch: str, out: Path) -> dict[str, Any]:
	out.mkdir(parents=True, exist_ok=True)
	pull = _gh_json([f"repos/{repo}/pulls/{pr}"])
	head = (pull or {}).get("head") or {}
	head_sha = head.get("sha") if isinstance(head.get("sha"), str) else ""
	if (head.get("repo") or {}).get("full_name") != repo or not SHA_RE.match(head_sha):
		raise FollowupError("head_unverified")
	merged_at = pull.get("merged_at") or ""

	run_pages = 0
	job_calls = 0
	checked: set[int] = set()
	runs_used: list[dict[str, Any]] = []
	selected: list[dict[str, Any]] = []

	def scan(runs: list[dict[str, Any]]) -> None:
		nonlocal job_calls
		for run in runs:
			if len(selected) >= MAX_LOGS or job_calls >= MAX_JOB_CALLS:
				return
			run_id = run.get("id")
			if not isinstance(run_id, int) or run_id < 1 or run_id in checked:
				continue
			checked.add(run_id)
			attempts = run.get("run_attempt") if isinstance(run.get("run_attempt"), int) and run["run_attempt"] > 0 else 1
			for attempt in range(1, attempts + 1):
				if len(selected) >= MAX_LOGS or job_calls >= MAX_JOB_CALLS:
					return
				job_calls += 1
				jobs = _gh_json([f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"])
				for job in (jobs or {}).get("jobs") or []:
					if not isinstance(job, dict) or not isinstance(job.get("id"), int):
						continue
					if not JOB_NAME_RE.match(job.get("name") or "") or job.get("conclusion") not in ("failure", "timed_out"):
						continue
					if len(selected) >= MAX_LOGS:
						break
					if not any(r["id"] == run_id and r["attempt"] == attempt for r in runs_used):
						runs_used.append({"id": run_id, "attempt": attempt, "html_url": run.get("html_url") or ""})
					selected.append({"id": job["id"], "run": run_id, "name": job.get("name"), "html_url": job.get("html_url") or ""})

	listing = _gh_json([f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={head_sha}&per_page=100"])
	run_pages += 1
	scan(sorted((listing or {}).get("workflow_runs") or [], key=lambda r: r.get("created_at") or "", reverse=True))
	page = 1
	while not selected and run_pages < MAX_RUN_PAGES and job_calls < MAX_JOB_CALLS:
		listing = _gh_json([f"repos/{repo}/actions/workflows/ci.yml/runs?branch={branch}&event=pull_request&per_page=100&page={page}"])
		run_pages += 1
		page += 1
		runs = [r for r in (listing or {}).get("workflow_runs") or [] if isinstance(r, dict) and (not merged_at or (r.get("created_at") or "") <= merged_at)]
		if not (listing or {}).get("workflow_runs"):
			break
		scan(sorted(runs, key=lambda r: r.get("created_at") or "", reverse=True))

	log_files: list[Path] = []
	jobs_out: list[dict[str, Any]] = []
	for job in selected:
		rc, raw, err = _gh_raw([f"repos/{repo}/actions/jobs/{job['id']}/logs"])
		status = "ok"
		if rc != 0:
			status = "expired" if re.search(r"\b(404|410)\b|Not Found|Gone", err) else "unavailable"
		else:
			path = out / f"run-{job['run']}-job-{job['id']}.txt"
			path.write_bytes(_redact(raw))
			log_files.append(path)
		jobs_out.append({"id": job["id"], "run": job["run"], "name": job["name"], "html_url": job["html_url"], "log_status": status})

	names = _failing_tests_from_logs(log_files)
	if names:
		fallback_reason = ""
	elif not selected:
		fallback_reason = "no_failed_orchestrate_poll_job_found"
	elif not log_files:
		fallback_reason = "job_logs_unavailable"
	else:
		fallback_reason = "no_failing_test_names_in_logs"
	evidence = {
		"head_sha": head_sha,
		"merged_at": merged_at,
		"source": "log" if names else "fallback",
		"fallback_reason": fallback_reason,
		"runs": runs_used,
		"jobs": jobs_out,
		"names": names,
	}
	(out / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
	return evidence


def cmd_locate(args: argparse.Namespace) -> int:
	try:
		evidence = locate(args.repo, args.pr, args.branch, Path(args.out))
	except FollowupError as exc:
		reason = "head_unverified" if str(exc) == "head_unverified" else "locate_failed"
		_log("failed", reason, detail=str(exc).replace(" ", "_"))
		return 1
	_log("located", evidence["source"], head_sha=evidence["head_sha"], names=len(evidence["names"]), jobs=len(evidence["jobs"]))
	_write_output(source=evidence["source"], head_sha=evidence["head_sha"], names_count=len(evidence["names"]))
	return 0


# --------------------------------------------------------------------- rerun

def module_test_names(module_path: Path) -> list[str]:
	tree = ast.parse(module_path.read_text(encoding="utf-8"))
	return sorted(node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"))


def nr_partition(names: list[str], total: int, index: int) -> list[str]:
	"""Same selection as ``awk -v n=index -v total=total 'NR % total == n'``."""
	return [name for line_no, name in enumerate(names, start=1) if line_no % total == index]


def ci_partition(names: list[str], groups: int = GROUPS, shards: int = SHARDS) -> dict[str, Any]:
	fast_fail = [n for n in names if n.startswith(FAST_FAIL_PREFIX)]
	remaining = [n for n in names if not n.startswith(FAST_FAIL_PREFIX)]
	layout = []
	for group in range(groups):
		group_names = nr_partition(remaining, groups, group)
		layout.append({"group": group, "shards": [nr_partition(group_names, shards, s) for s in range(shards)]})
	return {"fast_fail": fast_fail, "groups": layout}


def _start(cwd: Path, names: list[str], log_path: Path) -> subprocess.Popen:
	env = {k: v for k, v in os.environ.items() if not re.search(r"(TOKEN|SECRET|_PAT|API_KEY)$", k)}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	handle = open(log_path, "wb")
	proc = subprocess.Popen([sys.executable, POLL_MODULE, *names], cwd=str(cwd), stdout=handle, stderr=subprocess.STDOUT, env=env)
	proc._pr6576_log = handle  # type: ignore[attr-defined]
	return proc


def _finish(proc: subprocess.Popen, timeout: int) -> int:
	try:
		rc = proc.wait(timeout=timeout)
	except subprocess.TimeoutExpired:
		proc.kill()
		proc.wait()
		rc = 124
	proc._pr6576_log.close()  # type: ignore[attr-defined]
	return rc


def _run_batch(cwd: Path, batches: list[tuple[str, list[str]]], out: Path, timeout: int) -> list[dict[str, Any]]:
	started = []
	for label, names in batches:
		if not names:
			continue
		log_path = out / f"{label}.log"
		started.append((label, names, log_path, _start(cwd, names, log_path)))
	results = []
	for label, names, log_path, proc in started:
		rc = _finish(proc, timeout)
		failed = [n for n in _failing_tests_from_logs([log_path]) if n in names]
		results.append({"label": label, "rc": rc, "tests": len(names), "failed": failed, "process_failure": rc != 0 and not failed, "log": log_path.name})
	return results


def _excerpt(log_path: Path, name: str) -> list[str]:
	try:
		lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
	except OSError:
		return []
	for index, line in enumerate(lines):
		if name in line and ("FAIL" in line or "ERROR" in line or "Traceback" in line):
			return lines[index:index + EXCERPT_LINES]
	return []


def rerun(evidence: dict[str, Any], main_dir: Path, old_dir: Path | None, out: Path, timeout: int) -> dict[str, Any]:
	out.mkdir(parents=True, exist_ok=True)
	head_sha = evidence.get("head_sha") or ""
	if not SHA_RE.match(head_sha):
		raise FollowupError("head_sha_invalid")
	source = evidence.get("source")
	results: dict[str, Any] = {"source": source, "head_sha": head_sha, "groups": [], "old_head_failures": [], "old_head_process_failure": False}
	candidates = _safe_names(evidence.get("names"))
	if source == "fallback":
		if old_dir is None:
			raise FollowupError("old_dir_required")
		try:
			layout = ci_partition(module_test_names(old_dir / POLL_MODULE))
		except (OSError, SyntaxError):
			results["old_head_process_failure"] = True
			layout = {"fast_fail": [], "groups": []}
		if layout["fast_fail"]:
			ff = _run_batch(old_dir, [("old-fast-fail", layout["fast_fail"])], out, timeout)
			results["fast_fail"] = ff[0] if ff else None
		for group in layout["groups"]:
			shard_results = _run_batch(old_dir, [(f"old-g{group['group']}-s{s}", names) for s, names in enumerate(group["shards"])], out, timeout)
			results["groups"].append({"group": group["group"], "shards": shard_results})
		old_failed: list[str] = []
		entries = [results.get("fast_fail")] + [s for g in results["groups"] for s in g["shards"]]
		for entry in entries:
			if not entry:
				continue
			old_failed.extend(n for n in entry["failed"] if n not in old_failed)
			if entry["process_failure"]:
				results["old_head_process_failure"] = True
		results["old_head_failures"] = old_failed
		candidates = old_failed
	main_names = set(module_test_names(main_dir / POLL_MODULE))
	present = [n for n in candidates if n in main_names]
	absent = [n for n in candidates if n not in main_names]
	attempts = []
	for attempt in (1, 2):
		if not present:
			break
		attempts.extend(_run_batch(main_dir, [(f"main-attempt-{attempt}", present)], out, timeout))
	fail_counts = {n: sum(1 for a in attempts if n in a["failed"]) for n in present}
	reproduced = [n for n in present if attempts and fail_counts[n] == len(attempts) == 2]
	flaky = [n for n in present if 0 < fail_counts[n] < 2]
	excerpts = {n: _excerpt(out / "main-attempt-1.log", n) for n in reproduced}
	results["main"] = {
		"attempts": attempts,
		"reproduced_on_main": reproduced,
		"flaky_on_main": flaky,
		"absent_on_main": absent,
		"process_failure": any(a["process_failure"] for a in attempts),
		"excerpts": excerpts,
	}
	(out / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
	return results


def cmd_rerun(args: argparse.Namespace) -> int:
	try:
		evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
		timeout = int(os.environ.get("PR6576_SHARD_TIMEOUT_SECONDS") or "1800")
		results = rerun(evidence, Path(args.main_dir), Path(args.old_dir) if args.old_dir else None, Path(args.out), timeout)
	except (FollowupError, OSError, ValueError) as exc:
		_log("failed", "rerun_failed", detail=str(exc).replace(" ", "_"))
		return 1
	main = results["main"]
	_log("rerun", results["source"] or "unknown", reproduced=len(main["reproduced_on_main"]), flaky=len(main["flaky_on_main"]), absent=len(main["absent_on_main"]))
	_write_output(reproduced_count=len(main["reproduced_on_main"]))
	return 0


# --------------------------------------------------------------- file-defect

def _run_url() -> str:
	server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
	repo = os.environ.get("GITHUB_REPOSITORY") or ""
	run_id = os.environ.get("GITHUB_RUN_ID") or ""
	return f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id.isdigit() else ""


def _source_links(evidence: dict[str, Any]) -> list[str]:
	lines = []
	for job in evidence.get("jobs") or []:
		url = job.get("html_url") if isinstance(job.get("html_url"), str) and job["html_url"].startswith("https://") else ""
		name = _neutralize(job.get("name"), 60)
		lines.append(f"- {name} (job {int(job.get('id') or 0)}, log `{_neutralize(job.get('log_status'), 20)}`) {url}".rstrip())
	return lines


def build_defect_body(results: dict[str, Any], evidence: dict[str, Any]) -> str:
	main = results.get("main") or {}
	names = _safe_names(main.get("reproduced_on_main"))
	parts = [
		"`tests/test_orchestrate_poll_process.py` tests fail on current `main` in both of two reruns.",
		"Found by the one-time PR #6576 `orchestrate-poll` follow-up (`.github/workflows/pr6576-shard-followup.yml`).",
		"",
		"Failing tests:",
		*[f"- `{n}`" for n in names],
		"",
		"Evidence:",
		*_source_links(evidence),
	]
	run_url = _run_url()
	if run_url:
		parts.append(f"- Follow-up run: {run_url}")
	budget = EXCERPT_TOTAL_BYTES
	excerpts = main.get("excerpts") or {}
	for name in names:
		lines = excerpts.get(name) or []
		if not lines or budget <= 0:
			continue
		text = _neutralize("\n".join(str(line) for line in lines[:EXCERPT_LINES]), budget)
		budget -= len(text)
		parts += ["", f"Log excerpt for `{name}` (main, attempt 1):", "```text", text, "```"]
	parts += ["", "Refs #7072", "Refs #6576", "", DEFECT_MARKER]
	return "\n".join(parts) + "\n"


def file_defect(repo: str, results: dict[str, Any], evidence: dict[str, Any]) -> str:
	if not _safe_names((results.get("main") or {}).get("reproduced_on_main")):
		return ""
	login = _login()
	issues = _gh_json([f"repos/{repo}/issues?creator={login}&state=all&since={DEFECT_SINCE}&per_page=100"], paginate=True)
	for issue in issues:
		if isinstance(issue, dict) and "pull_request" not in issue and DEFECT_MARKER in (issue.get("body") or ""):
			return str(int(issue["number"]))
	created = _gh_json(["-X", "POST", f"repos/{repo}/issues"], input_obj={
		"title": "CI: orchestrate-poll tests failing on main (PR #6576 follow-up)",
		"body": build_defect_body(results, evidence),
	})
	number = (created or {}).get("number")
	if not isinstance(number, int):
		raise FollowupError("issue create returned no number")
	return str(number)


def cmd_file_defect(args: argparse.Namespace) -> int:
	try:
		results = json.loads(Path(args.results).read_text(encoding="utf-8"))
		evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
		number = file_defect(args.repo, results, evidence)
	except (FollowupError, OSError, ValueError, KeyError, TypeError) as exc:
		_log("failed", "file_defect_failed", detail=str(exc).replace(" ", "_"))
		return 1
	if number:
		_log("filed", "reproduced_on_main", issue=number)
	_write_output(issue=number)
	return 0


# -------------------------------------------------------------------- report

def build_report(results: dict[str, Any], evidence: dict[str, Any], defect_issue: str = "") -> str:
	main = results.get("main") or {}
	source = results.get("source") or evidence.get("source") or "unknown"
	lines = [
		MARKER,
		"## PR #6576 `orchestrate-poll` follow-up result",
		"",
		f"- Evidence source: **{'failed job log' if source == 'log' else 'fallback: partition rerun at the recorded head'}**",
	]
	if evidence.get("fallback_reason"):
		lines.append(f"- Fallback reason: `{_neutralize(evidence['fallback_reason'], 80)}`")
	lines.append(f"- Recorded head of #6576: `{results.get('head_sha') if SHA_RE.match(str(results.get('head_sha') or '')) else 'unknown'}`")
	run_url = _run_url()
	if run_url:
		lines.append(f"- This run: {run_url}")
	links = _source_links(evidence)
	lines += ["", "Original CI jobs:", *(links or ["- none found"])]
	if source == "log":
		lines += ["", "Failing tests in the original log: " + (", ".join(f"`{n}`" for n in _safe_names(evidence.get("names"))) or "none")]
	else:
		lines += ["", "| Group | Shard | Tests | Exit | Failed |", "| --- | --- | --- | --- | --- |"]
		ff = results.get("fast_fail")
		if ff:
			lines.append(f"| 0 | fast-fail | {int(ff['tests'])} | {int(ff['rc'])} | {', '.join(_safe_names(ff['failed'])) or ('process failure' if ff['process_failure'] else '-')} |")
		for group in results.get("groups") or []:
			for shard_index, shard in enumerate(group.get("shards") or []):
				failed = ", ".join(_safe_names(shard.get("failed"))) or ("process failure" if shard.get("process_failure") else "-")
				lines.append(f"| {int(group['group'])} | {shard_index} | {int(shard['tests'])} | {int(shard['rc'])} | {failed} |")
		if results.get("old_head_process_failure"):
			lines.append("\nAt least one run at the recorded head failed without naming a test, so that part of the evidence is inconclusive.")
	lines += ["", "Rerun on current `main` (two attempts):"]
	for attempt_index, attempt in enumerate(main.get("attempts") or [], start=1):
		failed = ", ".join(_safe_names(attempt.get("failed"))) or ("process failure" if attempt.get("process_failure") else "none")
		lines.append(f"- Attempt {attempt_index}: exit {int(attempt.get('rc') or 0)}, failed: {failed}")
	if not main.get("attempts"):
		lines.append("- Nothing to rerun: no failing test names to confirm on main.")
	for key, label in (("reproduced_on_main", "Reproduced on main"), ("flaky_on_main", "Failed once on main (flaky, not filed)"), ("absent_on_main", "Not defined on main")):
		lines.append(f"- {label}: " + (", ".join(f"`{n}`" for n in _safe_names(main.get(key))) or "none"))
	lines.append("")
	if defect_issue and defect_issue.isdigit():
		lines.append(f"Reproduced defect filed as #{defect_issue}.")
	else:
		lines.append("No reproduced defect, so nothing was filed.")
	return "\n".join(lines) + "\n"


def cmd_report(args: argparse.Namespace) -> int:
	try:
		results = json.loads(Path(args.results).read_text(encoding="utf-8"))
		evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
		body = build_report(results, evidence, args.defect_issue or "")
		summary = os.environ.get("GITHUB_STEP_SUMMARY")
		if summary:
			with open(summary, "a", encoding="utf-8") as handle:
				handle.write(body)
		_gh_json(["-X", "POST", f"repos/{args.repo}/issues/{args.issue}/comments"], input_obj={"body": body})
	except (FollowupError, OSError, ValueError, KeyError, TypeError) as exc:
		_log("failed", "report_failed", detail=str(exc).replace(" ", "_"))
		return 1
	_log("reported", results.get("source") or "unknown", issue=args.issue)
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
	sub = parser.add_subparsers(dest="command", required=True)
	p = sub.add_parser("guard")
	p.add_argument("--repo", required=True)
	p.add_argument("--issue", required=True)
	p.set_defaults(func=cmd_guard)
	p = sub.add_parser("locate")
	p.add_argument("--repo", required=True)
	p.add_argument("--pr", required=True)
	p.add_argument("--branch", required=True)
	p.add_argument("--out", required=True)
	p.set_defaults(func=cmd_locate)
	p = sub.add_parser("rerun")
	p.add_argument("--evidence", required=True)
	p.add_argument("--main-dir", required=True)
	p.add_argument("--old-dir", default="")
	p.add_argument("--out", required=True)
	p.set_defaults(func=cmd_rerun)
	p = sub.add_parser("file-defect")
	p.add_argument("--repo", required=True)
	p.add_argument("--results", required=True)
	p.add_argument("--evidence", required=True)
	p.set_defaults(func=cmd_file_defect)
	p = sub.add_parser("report")
	p.add_argument("--repo", required=True)
	p.add_argument("--issue", required=True)
	p.add_argument("--results", required=True)
	p.add_argument("--evidence", required=True)
	p.add_argument("--defect-issue", default="")
	p.set_defaults(func=cmd_report)
	args = parser.parse_args(argv)
	for name in ("repo", "issue", "pr"):
		value = getattr(args, name, None)
		if value is not None and not re.match(r"^(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|[0-9]+)$", value):
			parser.error(f"invalid --{name}")
	if getattr(args, "branch", None) is not None and not re.match(r"^[A-Za-z0-9_./-]+$", args.branch):
		parser.error("invalid --branch")
	return args.func(args)


if __name__ == "__main__":
	raise SystemExit(main())
