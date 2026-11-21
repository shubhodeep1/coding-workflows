#!/usr/bin/env python3
"""Early warning for the two CI budgets this repository has hit without notice.

Runs as the `budget-watch` job of `.github/workflows/ci.yml` on pushes to the
default branch, after the test jobs. It checks:

* workflow file size: any `.github/workflows/*.yml` at or above
  CI_BUDGET_WORKFLOW_WARN_BYTES (default 440,000; the hard guard in
  tests/test_workflow_file_size_limit.py fails at 480,000 and GitHub stops
  starting runs above 512,000, CLAUDE.md §27);
* CI job duration: any job of this CI run whose duration reached
  CI_BUDGET_JOB_WARN_RATIO (default 0.75) of its `timeout-minutes`. The
  duration runs from the job's first started step to its completion, so time
  spent waiting for a runner is not counted, and a job that ran no step (for
  example one cancelled while queued) is not reported (#9559).

For every finding it prints a `::warning::` and makes sure exactly one open
issue labelled `ai:ci-budget` exists for it (deduplicated by a hidden marker
`<!-- ai:ci-budget:v1 kind=<kind> subject=<subject> -->`). The issue is opened
with CI_BUDGET_ISSUE_TOKEN (a user PAT; default empty falls back to the job
token, which starts no `issues: opened` workflow), so the clarify -> plan ->
implement pipeline picks it up and does the split or rebalance before the
hard limit breaks CI.

Re-file cooldown: `orchestrate-poll` got a new issue three times in one week,
each a few hours after the previous one closed, for a job that sits just over
the warning ratio (15m16s of 20 minutes). When an issue for the same finding
was closed within CI_BUDGET_REFILE_COOLDOWN_DAYS (default 7; 0 turns the
cooldown off), a new one is opened only once the value reaches
CI_BUDGET_REFILE_URGENT_RATIO (default 0.9) of the limit. The `::warning::`
line is printed either way.

API calls (CLAUDE.md §15): one `actions/runs/{run_id}/jobs` read (skipped when
no run id), one open-issue listing for the label, one label create when a
new finding needs an issue (HTTP 422 when it exists), one listing of the 50
most recently updated closed issues for the label (only when a finding has no
open issue and the cooldown is on), and one create per new finding.
Fail-open: any API error is a warning; the job never fails, and a failed
closed-issue listing opens the issue as before.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LABEL = "ai:ci-budget"
DEFAULT_WORKFLOW_WARN_BYTES = 440_000
HARD_WORKFLOW_GUARD_BYTES = 480_000
DEFAULT_JOB_WARN_RATIO = 0.75
DEFAULT_REFILE_COOLDOWN_DAYS = 7
DEFAULT_REFILE_URGENT_RATIO = 0.9


def marker(kind: str, subject: str) -> str:
	return f"<!-- ai:ci-budget:v1 kind={kind} subject={subject} -->"


def workflow_size_findings(workflows_dir: Path, warn_bytes: int) -> list[dict[str, Any]]:
	findings = []
	for path in sorted(workflows_dir.glob("*.yml")):
		size = path.stat().st_size
		if size >= warn_bytes:
			findings.append({"kind": "workflow_size", "subject": f".github/workflows/{path.name}", "value": size, "limit": HARD_WORKFLOW_GUARD_BYTES})
	return findings


def job_timeouts(ci_workflow: Path) -> dict[str, int]:
	"""Job id and display name -> timeout-minutes, for jobs that set one."""
	# Imported here so a missing PyYAML only skips the job-duration check
	# (main() catches the ImportError); the workflow-size check needs no YAML.
	import yaml

	data = yaml.safe_load(ci_workflow.read_text(encoding="utf-8")) or {}
	out: dict[str, int] = {}
	for job_id, job in (data.get("jobs") or {}).items():
		minutes = job.get("timeout-minutes") if isinstance(job, dict) else None
		if isinstance(minutes, int) and minutes > 0:
			out[job_id] = minutes
			name = job.get("name")
			if isinstance(name, str) and name:
				out[name] = minutes
	return out


def _seconds(start: str | None, end: str | None) -> int | None:
	# A non-string value (only possible with malformed API data) would raise
	# AttributeError at `.replace` and stop the duration check for every job.
	if not isinstance(start, str) or not isinstance(end, str) or not start or not end:
		return None
	try:
		a = datetime.fromisoformat(start.replace("Z", "+00:00"))
		b = datetime.fromisoformat(end.replace("Z", "+00:00"))
		return max(0, int((b - a).total_seconds()))
	except (TypeError, ValueError):
		# TypeError: a naive time mixed with an aware one; not checked.
		return None


def _job_run_start(job: dict[str, Any]) -> str | None:
	"""When the job actually started running, excluding the wait for a runner.

	No `steps` list in the response (older data, partial responses): the job's
	own `started_at`, as before. A `steps` list: the earliest non-empty step
	`started_at`. A `steps` list in which no step started: None, because the
	job never ran (#9559: jobs cancelled while queued reported 33m49s of a
	20-minute timeout).
	"""
	steps = job.get("steps")
	if not isinstance(steps, list):
		return job.get("started_at")
	# Only non-empty string times count as a started step, so a malformed
	# (for example numeric) start is not mistaken for one.
	starts = [step.get("started_at") for step in steps if isinstance(step, dict) and isinstance(step.get("started_at"), str) and step.get("started_at")]
	if not starts:
		return None
	# Compare parsed times, not strings, so mixed offsets order correctly. Each
	# start is parsed on its own and an unparsable one is dropped, so one
	# malformed step time cannot move the start later. A naive time is taken as
	# UTC (GitHub emits `Z` times). When no start parses, the first is returned
	# and `_seconds` rejects it, so the duration is not checked.
	_parsed_starts: list[datetime] = []
	for value in starts:
		try:
			when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
		except ValueError:
			continue
		_parsed_starts.append(when if when.tzinfo else when.replace(tzinfo=timezone.utc))
	if not _parsed_starts:
		return starts[0]
	return min(_parsed_starts).isoformat()


def job_duration_findings(jobs: list[dict[str, Any]], timeouts: dict[str, int], ratio: float) -> list[dict[str, Any]]:
	"""Jobs whose duration reached ratio * timeout. Matrix jobs `name (x)` use the base name's timeout.

	The duration runs from the first started step to the job's completion; jobs
	that ran no step are not reported (they hit no timeout while running).
	"""
	findings = []
	for job in jobs:
		name = str(job.get("name") or "")
		base = name.split(" (", 1)[0]
		minutes = timeouts.get(name) or timeouts.get(base)
		if not minutes:
			continue
		if job.get("conclusion") == "skipped":
			continue
		run_start = _job_run_start(job)
		if run_start is None and isinstance(job.get("steps"), list):
			# Only a finished job is logged: a job still queued (no
			# completed_at) has not run yet rather than "ran no step".
			if job.get("completed_at"):
				# Escape workflow-command data so a name cannot end the command line.
				safe_name = name.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
				print(f"::notice::ci_budget_watch: job {safe_name} ran no step (conclusion={job.get('conclusion')}); duration not checked.")
			continue
		took = _seconds(run_start, job.get("completed_at"))
		if took is None:
			# A finished job whose start or completion time is missing or does
			# not parse is logged rather than skipped silently; a job still
			# running (no completed_at) is not.
			if job.get("completed_at"):
				unmeasured_name = name.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
				print(f"::notice::ci_budget_watch: job {unmeasured_name} has no usable start or completion time (conclusion={job.get('conclusion')}); duration not checked.")
			continue
		if took >= ratio * minutes * 60:
			findings.append({"kind": "job_duration", "subject": base, "value": took, "limit": minutes * 60, "job": name})
	return findings


def issue_title(finding: dict[str, Any]) -> str:
	if finding["kind"] == "workflow_size":
		return f"CI budget: {finding['subject']} is {finding['value']:,} bytes (guard {finding['limit']:,})"
	return f"CI budget: CI job {finding['subject']} took {finding['value'] // 60}m{finding['value'] % 60:02d}s of its {finding['limit'] // 60}-minute timeout"


def issue_body(finding: dict[str, Any], run_url: str) -> str:
	if finding["kind"] == "workflow_size":
		task = (
			f"`{finding['subject']}` is {finding['value']:,} bytes. `tests/test_workflow_file_size_limit.py` fails at "
			f"{HARD_WORKFLOW_GUARD_BYTES:,} bytes and GitHub stops starting runs above 512,000 bytes.\n\n"
			"Move the largest inline `run:` bodies into `scripts/` until the file is at least 50,000 bytes under the guard, "
			"following CLAUDE.md §27 and agents.md \"Workflow file size limit\" (for `review_autofix.yml`: "
			"`scripts/review_autofix_step_<slug>.sh`, the resolving wrapper, `REQUIRED_BOOTSTRAP_SCRIPTS`, "
			"`tests/review_autofix_step_scripts.py` and `docs/INVENTORY.md`). Keep every step's `name:`, `id:`, `if:`, `env:` "
			"and `continue-on-error:`, and switch tests that read moved bodies to `expanded_review_autofix_text()`. "
			"Never raise the guard."
		)
	else:
		job = finding.get("job", finding["subject"])
		task = (
			f"CI job `{job}` in `.github/workflows/ci.yml` took {finding['value'] // 60}m{finding['value'] % 60:02d}s of its "
			f"{finding['limit'] // 60}-minute `timeout-minutes`. A job that reaches its timeout is cancelled and turns CI red with no code fault.\n\n"
			"Move its slowest steps (see the run's step timings) into the CI job with the most headroom, unchanged, keeping job and "
			"step names. Do not raise `timeout-minutes` and do not split tests into new required job names."
		)
	return (
		f"{marker(finding['kind'], finding['subject'])}\n"
		"Opened automatically by the `budget-watch` job in `.github/workflows/ci.yml` "
		f"(`scripts/ci_budget_watch.py`) on {run_url}.\n\n{task}\n"
	)


def recently_closed_issue(closed_issues: list[Any], finding: dict[str, Any], *, cooldown_days: int, now: datetime) -> int | None:
	"""Number of the newest issue for this finding closed within the cooldown, else None."""
	if cooldown_days <= 0:
		return None
	wanted = marker(finding["kind"], finding["subject"])
	newest: tuple[datetime, int] | None = None
	for issue in closed_issues:
		if not isinstance(issue, dict) or issue.get("pull_request") is not None or wanted not in str(issue.get("body") or ""):
			continue
		closed_at, number = issue.get("closed_at"), issue.get("number")
		if not isinstance(closed_at, str) or not isinstance(number, int):
			continue
		try:
			when = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
		except ValueError:
			continue
		when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
		if (now - when).total_seconds() <= cooldown_days * 86400 and (newest is None or when > newest[0]):
			newest = (when, number)
	return newest[1] if newest else None


def _gh(args: list[str], *, data: dict[str, Any] | None = None, token: str = "") -> Any:
	cmd = ["gh", "api", *args]
	if data is not None:
		cmd += ["--input", "-"]
	env = {**os.environ, "GH_TOKEN": token} if token else None
	res = subprocess.run(cmd, input=json.dumps(data) if data is not None else None, capture_output=True, text=True, timeout=60, env=env)
	if res.returncode != 0:
		raise RuntimeError(res.stderr.strip()[:300])
	return json.loads(res.stdout or "null")


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
	parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
	parser.add_argument("--root", default=".")
	parser.add_argument("--dry-run", action="store_true", help="print findings only; open no issues")
	args = parser.parse_args(argv)
	root = Path(args.root)
	try:
		warn_bytes = int(os.environ.get("CI_BUDGET_WORKFLOW_WARN_BYTES", DEFAULT_WORKFLOW_WARN_BYTES))
	except ValueError:
		warn_bytes = DEFAULT_WORKFLOW_WARN_BYTES
	# A threshold at or above the hard guard would only warn after CI already
	# fails, so it falls back to the default instead of silently disabling the
	# early warning.
	if not 0 < warn_bytes < HARD_WORKFLOW_GUARD_BYTES:
		print(f"::warning::ci_budget_watch: CI_BUDGET_WORKFLOW_WARN_BYTES={warn_bytes} is outside (0, {HARD_WORKFLOW_GUARD_BYTES}); using {DEFAULT_WORKFLOW_WARN_BYTES}.")
		warn_bytes = DEFAULT_WORKFLOW_WARN_BYTES
	try:
		ratio = float(os.environ.get("CI_BUDGET_JOB_WARN_RATIO", DEFAULT_JOB_WARN_RATIO))
	except ValueError:
		ratio = DEFAULT_JOB_WARN_RATIO
	# A ratio of 1 would only warn once the job already hit its timeout.
	if not 0 < ratio < 1:
		ratio = DEFAULT_JOB_WARN_RATIO
	try:
		cooldown_days = int(os.environ.get("CI_BUDGET_REFILE_COOLDOWN_DAYS", DEFAULT_REFILE_COOLDOWN_DAYS))
	except ValueError:
		cooldown_days = DEFAULT_REFILE_COOLDOWN_DAYS
	if not 0 <= cooldown_days <= 90:
		cooldown_days = DEFAULT_REFILE_COOLDOWN_DAYS
	try:
		urgent_ratio = float(os.environ.get("CI_BUDGET_REFILE_URGENT_RATIO", DEFAULT_REFILE_URGENT_RATIO))
	except ValueError:
		urgent_ratio = DEFAULT_REFILE_URGENT_RATIO
	if not 0 < urgent_ratio <= 1:
		urgent_ratio = DEFAULT_REFILE_URGENT_RATIO

	findings = workflow_size_findings(root / ".github" / "workflows", warn_bytes)
	if args.repo and args.run_id:
		jobs = None
		try:
			jobs = _gh([f"repos/{args.repo}/actions/runs/{args.run_id}/jobs?per_page=100"]).get("jobs", [])
		except Exception as exc:  # noqa: BLE001 - fail-open by contract
			print(f"::warning::ci_budget_watch: could not read this run's jobs ({exc}); job durations not checked.")
		if jobs is not None:
			# Separate from the jobs read so a missing PyYAML or a malformed
			# ci.yml is not reported as an API failure.
			try:
				findings += job_duration_findings(jobs, job_timeouts(root / ".github" / "workflows" / "ci.yml"), ratio)
			except Exception as exc:  # noqa: BLE001 - fail-open by contract
				print(f"::warning::ci_budget_watch: could not read job timeouts from ci.yml ({exc}); job durations not checked.")

	for f in findings:
		print(f"::warning::CI_BUDGET kind={f['kind']} subject={f['subject']} value={f['value']} limit={f['limit']}")
	print(f"CI_BUDGET findings={len(findings)} warn_bytes={warn_bytes} job_ratio={ratio}")
	if not findings or args.dry_run or not args.repo:
		return 0

	run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{args.repo}/actions/runs/{args.run_id}"
	try:
		open_issues = _gh([f"repos/{args.repo}/issues?state=open&labels={LABEL}&per_page=100"]) or []
	except Exception as exc:  # noqa: BLE001
		print(f"::warning::ci_budget_watch: could not list open {LABEL} issues ({exc}); not opening any.")
		return 0
	existing = "\n".join(str(issue.get("body") or "") for issue in open_issues)
	if any(marker(f["kind"], f["subject"]) not in existing for f in findings):
		# The open-issue listing filters on LABEL, so an issue whose label was
		# dropped would be invisible to the dedup and re-opened on every push.
		# Create the label first; "already exists" (HTTP 422) is the normal case.
		try:
			_gh([f"repos/{args.repo}/labels", "-X", "POST"], data={"name": LABEL, "color": "d93f0b", "description": "CI budget early warning (scripts/ci_budget_watch.py)"})
			print(f"CI_BUDGET_LABEL name={LABEL} action=created")
		except Exception as exc:  # noqa: BLE001 - fail-open; 422 means it exists
			if "already_exists" not in str(exc) and "422" not in str(exc):
				print(f"::warning::ci_budget_watch: could not ensure the {LABEL} label ({exc}).")
	# Read at most once, and only when a finding has no open issue.
	closed_issues: list[Any] | None = None
	for f in findings:
		if marker(f["kind"], f["subject"]) in existing:
			print(f"CI_BUDGET_ISSUE kind={f['kind']} subject={f['subject']} action=exists")
			continue
		if cooldown_days > 0 and f["value"] < urgent_ratio * f["limit"]:
			if closed_issues is None:
				try:
					closed_issues = _gh([f"repos/{args.repo}/issues?state=closed&labels={LABEL}&sort=updated&direction=desc&per_page=50"]) or []
				except Exception as exc:  # noqa: BLE001 - fail-open: open the issue as before
					print(f"::warning::ci_budget_watch: could not list closed {LABEL} issues ({exc}); re-file cooldown not applied.")
					closed_issues = []
			recent = recently_closed_issue(closed_issues if isinstance(closed_issues, list) else [], f, cooldown_days=cooldown_days, now=datetime.now(timezone.utc))
			if recent is not None:
				print(f"CI_BUDGET_ISSUE kind={f['kind']} subject={f['subject']} action=cooldown closed_issue={recent} cooldown_days={cooldown_days} urgent_ratio={urgent_ratio}")
				continue
		try:
			# GitHub starts no workflow for an issue opened with the job token,
			# so the create uses CI_BUDGET_ISSUE_TOKEN (a user PAT) when set,
			# which lets `issues: opened` route the issue into clarify.
			created = _gh(
				[f"repos/{args.repo}/issues", "-X", "POST"],
				data={"title": issue_title(f), "body": issue_body(f, run_url), "labels": [LABEL]},
				token=os.environ.get("CI_BUDGET_ISSUE_TOKEN", ""),
			)
			print(f"CI_BUDGET_ISSUE kind={f['kind']} subject={f['subject']} action=opened number={created.get('number')}")
			# Matrix jobs share a base-name subject, so later findings with the
			# same marker in this run must see the issue just opened.
			existing += "\n" + marker(f["kind"], f["subject"])
		except Exception as exc:  # noqa: BLE001
			print(f"::warning::ci_budget_watch: could not open the {f['kind']} issue for {f['subject']} ({exc}).")
	return 0


if __name__ == "__main__":
	sys.exit(main())
