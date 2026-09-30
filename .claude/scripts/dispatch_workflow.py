#!/usr/bin/env python3
"""Dispatch one allowlisted workflow and print the id of the run it started.

`/implement-plan-claude` dispatches the security audit, the validation run,
and the review-convergence run, then records the run id it waits on. The old
recipe (`gh workflow run` then `gh run list -L 1`) can return the previous
run when the new one has not been created yet, so stage sessions wrote their
own `for` loops with `sleep` and `$(gh api ...)` to wait for it. Those
multi-part commands stopped unattended sessions at permission prompts. This
helper does the dispatch and the wait in one allowlisted command.

Usage:

  dispatch_workflow.py --repo OWNER/REPO --workflow FILE [--ref REF]
                       [--input KEY=VALUE ...] [--timeout-seconds N]

FILE must be one of DISPATCHABLE_WORKFLOWS: the six files
`.claude/settings.json` pre-approves as `Bash(gh workflow run <file> *)`
(CLAUDE.md §23.C command-invoked carve-out), plus DEFAULT_BRANCH_ONLY_WORKFLOWS;
any other file is refused without an API call. `--ref` defaults to the
repository's default branch.

DEFAULT_BRANCH_ONLY_WORKFLOWS (`internal-review.yml`) have no allow rule: this
helper is their only pre-approved dispatch path, and it never runs a pushable
branch's copy of them with the workflow's secrets (issue #5375, the same rule
review_autofix_sweep.yml follows since issue #4618). For them it always reads
the default branch from the API and dispatches on it, refuses any other
`--ref`, and accepts only `pr_number`, as a positive decimal integer. Those
refusals exit 1 before any POST.

API calls (CLAUDE.md §15), all REST:
  - one read of the repository when `--ref` is omitted, or the workflow is in
    DEFAULT_BRANCH_ONLY_WORKFLOWS;
  - one read of the workflow's recent `workflow_dispatch` runs before the
    dispatch, to know which runs already existed;
  - one POST to `actions/workflows/<file>/dispatches`;
  - one read of the recent runs every POLL_INTERVAL_SECONDS until a run that
    did not exist before appears, at most `--timeout-seconds` (default 90)
    worth of polls.

Prints one JSON line. Exit 0 with `run_id`, `html_url`, `status`,
`created_at` when the new run was found; exit 1 when the workflow is not
allowlisted or an argument is invalid; exit 2 when a call failed or no new
run appeared before the timeout. On exit 2, `dispatched` says whether the
POST may have reached GitHub: once it is `true`, a failed or timed-out poll
never means "dispatch again", because the run already exists or may exist.
A POST that timed out, got a 5xx, or failed without an HTTP status counts as
`true`, since GitHub can accept it before the failure; only a POST GitHub
refused with a 4xx, or one `gh` could not start, reports `false`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_CHECKER_PATH = Path(__file__).resolve().with_name("check_in_status.py")
_checker_spec = importlib.util.spec_from_file_location("check_in_status", _CHECKER_PATH)
check_in_status = importlib.util.module_from_spec(_checker_spec)
_checker_spec.loader.exec_module(check_in_status)

# Keep equal to the `Bash(gh workflow run <file> *)` allow rules in
# `.claude/settings.json` plus DEFAULT_BRANCH_ONLY_WORKFLOWS;
# tests/test_dispatch_workflow.py asserts it.
DISPATCHABLE_WORKFLOWS = frozenset(
	{
		"security-audit.yml",
		"ai-security-audit.yml",
		"internal-validate.yml",
		"ai-validate.yml",
		"review_autofix.yml",
		"ai-review.yml",
		# /fix-claude-pr re-dispatches a stalled review with it (issue #4985):
		# only its dispatched runs are bound to their PR by title ([pr:N]).
		"internal-review.yml",
	}
)
# Dispatched only from the default branch the API reports, with a numeric
# `pr_number` and no other input (issue #5375). They have no allow rule, so
# this helper is their only pre-approved path; the gh api guard asks on them.
DEFAULT_BRANCH_ONLY_WORKFLOWS = frozenset({"internal-review.yml"})
DEFAULT_BRANCH_ONLY_INPUT_KEYS = frozenset({"pr_number"})
PR_NUMBER_INPUT_RE = re.compile(r"^[1-9][0-9]{0,9}$")
POLL_INTERVAL_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 90
RECENT_RUNS_PER_PAGE = 20
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
INPUT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _runs_path(repo: str, workflow: str) -> str:
	return f"repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page={RECENT_RUNS_PER_PAGE}"


def recent_run_ids(repo: str, workflow: str) -> set[int]:
	"""Ids of the workflow's most recent `workflow_dispatch` runs (one REST read)."""
	payload = check_in_status.gh_api(_runs_path(repo, workflow))
	return {run["id"] for run in payload.get("workflow_runs", []) if isinstance(run, dict) and isinstance(run.get("id"), int)}


def find_new_run(repo: str, workflow: str, known_ids: set[int]) -> dict | None:
	"""Return the newest run that is not in `known_ids`, or None (one REST read)."""
	payload = check_in_status.gh_api(_runs_path(repo, workflow))
	new_runs = [
		run
		for run in payload.get("workflow_runs", [])
		if isinstance(run, dict) and isinstance(run.get("id"), int) and run["id"] not in known_ids
	]
	if not new_runs:
		return None
	return max(new_runs, key=lambda run: run["id"])


class DispatchUnconfirmed(check_in_status.ReadError):
	"""The dispatch POST failed in a way GitHub may still have accepted it."""


# `gh api` ends an HTTP error with `(HTTP <status>)`; a 4xx means GitHub
# refused the request, so no run was created.
_HTTP_CLIENT_ERROR_RE = re.compile(r"\(HTTP 4\d\d\)")


def _post_dispatch(repo: str, workflow: str, ref: str, inputs: dict[str, str]) -> None:
	"""POST the dispatch; raise ReadError when refused, DispatchUnconfirmed when it may have landed."""
	body: dict[str, object] = {"ref": ref}
	if inputs:
		body["inputs"] = inputs
	payload_file = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
	payload_path = payload_file.name
	try:
		with payload_file:
			json.dump(body, payload_file)
		proc = subprocess.run(
			["gh", "api", "-X", "POST", f"repos/{repo}/actions/workflows/{workflow}/dispatches", "--input", payload_path],
			capture_output=True,
			text=True,
			timeout=60,
		)
	except subprocess.TimeoutExpired as exc:
		raise DispatchUnconfirmed(f"dispatch of {workflow} timed out: {exc}") from exc
	except (OSError, TypeError, ValueError) as exc:
		raise check_in_status.ReadError(f"dispatch of {workflow} failed: {exc}") from exc
	finally:
		Path(payload_path).unlink(missing_ok=True)
	if proc.returncode != 0:
		output = (proc.stderr or proc.stdout).strip()
		detail = output.splitlines()
		message = f"dispatch of {workflow} failed: {detail[-1] if detail else f'exit {proc.returncode}'}"
		if _HTTP_CLIENT_ERROR_RE.search(output):
			raise check_in_status.ReadError(message)
		raise DispatchUnconfirmed(message)


def parse_inputs(pairs: list[str]) -> dict[str, str]:
	"""Turn `KEY=VALUE` strings into a dict; raise ValueError on a bad pair."""
	inputs: dict[str, str] = {}
	for pair in pairs:
		key, separator, value = pair.partition("=")
		if not separator or not INPUT_KEY_RE.fullmatch(key):
			raise ValueError(f"--input must be KEY=VALUE, got {pair!r}")
		if key in inputs:
			raise ValueError(f"--input {key} given twice")
		inputs[key] = value
	return inputs


def validate_default_branch_only_inputs(workflow: str, inputs: dict[str, str]) -> None:
	"""Raise ValueError unless `inputs` is exactly a numeric `pr_number` (issue #5375)."""
	extra = sorted(set(inputs) - DEFAULT_BRANCH_ONLY_INPUT_KEYS)
	if extra:
		raise ValueError(f"{workflow} accepts only --input pr_number=<N>, not {', '.join(extra)}")
	if not PR_NUMBER_INPUT_RE.fullmatch(inputs.get("pr_number", "")):
		raise ValueError(f"{workflow} needs --input pr_number=<N> with N a positive decimal integer")


def dispatch(repo: str, workflow: str, ref: str | None, inputs: dict[str, str], timeout_seconds: int, sleep=time.sleep) -> tuple[int, dict]:
	"""Dispatch `workflow` on `repo` and wait for its new run."""
	if not REPO_RE.fullmatch(repo or ""):
		raise ValueError("--repo must be OWNER/REPO")
	if workflow not in DISPATCHABLE_WORKFLOWS:
		raise ValueError(f"{workflow} is not one of the dispatchable workflows: {', '.join(sorted(DISPATCHABLE_WORKFLOWS))}")
	if timeout_seconds < 0:
		raise ValueError("--timeout-seconds must not be negative")
	default_branch_only = workflow in DEFAULT_BRANCH_ONLY_WORKFLOWS
	if default_branch_only:
		validate_default_branch_only_inputs(workflow, inputs)
	if not ref or default_branch_only:
		default_branch = check_in_status.gh_api(f"repos/{repo}").get("default_branch") or ""
		if not isinstance(default_branch, str) or not default_branch:
			raise check_in_status.ReadError(f"could not read the default branch of {repo}")
		if ref and ref != default_branch:
			raise ValueError(f"{workflow} is dispatched only from the default branch {default_branch!r}, not --ref {ref!r}")
		ref = default_branch
	known_ids = recent_run_ids(repo, workflow)
	try:
		_post_dispatch(repo, workflow, ref, inputs)
	except DispatchUnconfirmed as exc:
		return 2, {
			"dispatched": True,
			"workflow": workflow,
			"ref": ref,
			"error": f"the dispatch may have reached GitHub ({exc}); check `gh run list --workflow={workflow}` before dispatching again",
		}
	# From here on the run exists (or will), so every failure reports
	# `dispatched: true`: a caller must look for the run, never dispatch again.
	attempts = max(1, timeout_seconds // POLL_INTERVAL_SECONDS)
	for attempt in range(attempts):
		sleep(POLL_INTERVAL_SECONDS)
		try:
			run = find_new_run(repo, workflow, known_ids)
		except (check_in_status.ReadError, KeyError, TypeError) as exc:
			return 2, {
				"dispatched": True,
				"workflow": workflow,
				"ref": ref,
				"error": f"dispatched, but reading the new {workflow} run failed ({exc}); check `gh run list --workflow={workflow}` before dispatching again",
			}
		if run is not None:
			return 0, {
				"dispatched": True,
				"workflow": workflow,
				"ref": ref,
				"run_id": run["id"],
				"html_url": run.get("html_url"),
				"status": run.get("status"),
				"created_at": run.get("created_at"),
			}
	return 2, {
		"dispatched": True,
		"workflow": workflow,
		"ref": ref,
		"error": f"no new {workflow} run appeared within {attempts * POLL_INTERVAL_SECONDS}s; check `gh run list --workflow={workflow}` before dispatching again",
	}


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--repo", required=True)
	parser.add_argument("--workflow", required=True)
	parser.add_argument("--ref")
	parser.add_argument("--input", action="append", default=[], metavar="KEY=VALUE")
	parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		code, result = dispatch(args.repo, args.workflow, args.ref, parse_inputs(args.input), args.timeout_seconds)
	except ValueError as exc:
		print(json.dumps({"dispatched": False, "error": str(exc)}))
		return 1
	except (check_in_status.ReadError, KeyError, TypeError) as exc:
		print(json.dumps({"dispatched": False, "error": str(exc)}))
		return 2
	print(json.dumps(result))
	return code


if __name__ == "__main__":
	sys.exit(main())
