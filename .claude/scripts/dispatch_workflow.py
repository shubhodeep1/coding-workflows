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

FILE must be one of DISPATCHABLE_WORKFLOWS, the same six files
`.claude/settings.json` pre-approves as `Bash(gh workflow run <file> *)`
(CLAUDE.md §23.C command-invoked carve-out); any other file is refused
without an API call. `--ref` defaults to the repository's default branch.

The POST sends `return_run_details: true`, so GitHub answers with the id
of the run this dispatch started (`workflow_run_id`). That id is exact even
when another session dispatches the same workflow seconds apart (issue
#5016: taking the newest new run returned the other session's run). Only
when the response carries no run id does the helper fall back to polling for
runs that did not exist before the POST, and then it never guesses between
several.

API calls (CLAUDE.md §15), all REST:
  - one read of the repository when `--ref` is omitted;
  - one read of the workflow's recent `workflow_dispatch` runs before the
    dispatch, to know which runs already existed (used by the fallback);
  - one POST to `actions/workflows/<file>/dispatches`;
  - with a run id in the response: one best-effort read of that run for
    `status` and `created_at` (both `null` when the read fails);
  - without one: one read of the recent runs every POLL_INTERVAL_SECONDS
    until a run that did not exist before appears, at most
    `--timeout-seconds` (default 90) worth of polls.

Prints one JSON line. Exit 0 with `run_id`, `html_url`, `status`,
`created_at`, and `matched_by` when the run was found: `dispatch_response`
means GitHub named the run; `new_run` means the fallback saw exactly one new
run, which may still be another session's, so the caller confirms its target
ref before trusting it. Exit 1 when the workflow is not allowlisted or an
argument is invalid. Exit 2 when a call failed, no new run appeared before
the timeout, or the fallback saw more than one new run (`ambiguous: true`
with `candidate_run_ids`, newest first). On exit 2, `dispatched` says whether
the POST may have reached GitHub: once it is `true`, a failed or timed-out
poll never means "dispatch again", because the run already exists or may
exist. A POST that timed out, got a 5xx, or failed without an HTTP status
counts as `true`, since GitHub can accept it before the failure; only a POST
GitHub refused with a 4xx, or one `gh` could not start, reports `false`.
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
# `.claude/settings.json`; tests/test_dispatch_workflow.py asserts it.
DISPATCHABLE_WORKFLOWS = frozenset(
	{
		"security-audit.yml",
		"ai-security-audit.yml",
		"internal-validate.yml",
		"ai-validate.yml",
		"review_autofix.yml",
		"ai-review.yml",
	}
)
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


def new_runs(repo: str, workflow: str, known_ids: set[int]) -> list[dict]:
	"""Return every run that is not in `known_ids`, newest first (one REST read)."""
	payload = check_in_status.gh_api(_runs_path(repo, workflow))
	fresh = [
		run
		for run in payload.get("workflow_runs", [])
		if isinstance(run, dict) and isinstance(run.get("id"), int) and run["id"] not in known_ids
	]
	return sorted(fresh, key=lambda run: run["id"], reverse=True)


def find_new_run(repo: str, workflow: str, known_ids: set[int]) -> dict | None:
	"""Return the newest run that is not in `known_ids`, or None (one REST read)."""
	fresh = new_runs(repo, workflow, known_ids)
	return fresh[0] if fresh else None


def _response_run_id(response: dict | None) -> int | None:
	"""The run id GitHub returned for the dispatch, or None when it sent none."""
	if not isinstance(response, dict):
		return None
	run_id = response.get("workflow_run_id")
	if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
		return None
	return run_id


def _read_run(repo: str, run_id: int) -> dict:
	"""Best-effort read of one run; an empty dict when the read fails."""
	try:
		run = check_in_status.gh_api(f"repos/{repo}/actions/runs/{run_id}")
	except (check_in_status.ReadError, KeyError, TypeError):
		return {}
	return run if isinstance(run, dict) else {}


class DispatchUnconfirmed(check_in_status.ReadError):
	"""The dispatch POST failed in a way GitHub may still have accepted it."""


# `gh api` ends an HTTP error with `(HTTP <status>)`; a 4xx means GitHub
# refused the request, so no run was created.
_HTTP_CLIENT_ERROR_RE = re.compile(r"\(HTTP 4\d\d\)")


def _post_dispatch(repo: str, workflow: str, ref: str, inputs: dict[str, str]) -> dict | None:
	"""POST the dispatch and return GitHub's response object, or None when it sent no JSON object.

	Raise ReadError when GitHub refused it, DispatchUnconfirmed when it may have landed.
	"""
	body: dict[str, object] = {"ref": ref, "return_run_details": True}
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
	# A 204 (no `return_run_details` support) prints nothing; anything that is
	# not a JSON object is treated the same way, so the caller falls back.
	try:
		response = json.loads(proc.stdout or "")
	except (TypeError, ValueError):
		return None
	return response if isinstance(response, dict) else None


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


def dispatch(repo: str, workflow: str, ref: str | None, inputs: dict[str, str], timeout_seconds: int, sleep=time.sleep) -> tuple[int, dict]:
	"""Dispatch `workflow` on `repo` and wait for its new run."""
	if not REPO_RE.fullmatch(repo or ""):
		raise ValueError("--repo must be OWNER/REPO")
	if workflow not in DISPATCHABLE_WORKFLOWS:
		raise ValueError(f"{workflow} is not one of the dispatchable workflows: {', '.join(sorted(DISPATCHABLE_WORKFLOWS))}")
	if timeout_seconds < 0:
		raise ValueError("--timeout-seconds must not be negative")
	if not ref:
		ref = check_in_status.gh_api(f"repos/{repo}").get("default_branch") or ""
		if not ref:
			raise check_in_status.ReadError(f"could not read the default branch of {repo}")
	known_ids = recent_run_ids(repo, workflow)
	try:
		response = _post_dispatch(repo, workflow, ref, inputs)
	except DispatchUnconfirmed as exc:
		return 2, {
			"dispatched": True,
			"workflow": workflow,
			"ref": ref,
			"error": f"the dispatch may have reached GitHub ({exc}); check `gh run list --workflow={workflow}` before dispatching again",
		}
	# From here on the run exists (or will), so every failure reports
	# `dispatched: true`: a caller must look for the run, never dispatch again.
	response_run_id = _response_run_id(response)
	if response_run_id is not None:
		run = _read_run(repo, response_run_id)
		return 0, {
			"dispatched": True,
			"workflow": workflow,
			"ref": ref,
			"run_id": response_run_id,
			"html_url": response.get("html_url") or run.get("html_url"),
			"status": run.get("status"),
			"created_at": run.get("created_at"),
			"matched_by": "dispatch_response",
		}
	attempts = max(1, timeout_seconds // POLL_INTERVAL_SECONDS)
	for attempt in range(attempts):
		sleep(POLL_INTERVAL_SECONDS)
		try:
			fresh = new_runs(repo, workflow, known_ids)
		except (check_in_status.ReadError, KeyError, TypeError) as exc:
			return 2, {
				"dispatched": True,
				"workflow": workflow,
				"ref": ref,
				"error": f"dispatched, but reading the new {workflow} run failed ({exc}); check `gh run list --workflow={workflow}` before dispatching again",
			}
		if len(fresh) > 1:
			# Another dispatch of the same workflow landed in the same window
			# and nothing tells the runs apart: report them, never pick one.
			return 2, {
				"dispatched": True,
				"workflow": workflow,
				"ref": ref,
				"ambiguous": True,
				"candidate_run_ids": [run["id"] for run in fresh],
				"error": f"dispatched, but {len(fresh)} new {workflow} runs appeared and GitHub returned no run id; confirm which candidate ran this dispatch's inputs before recording one, and never dispatch again",
			}
		if fresh:
			run = fresh[0]
			return 0, {
				"dispatched": True,
				"workflow": workflow,
				"ref": ref,
				"run_id": run["id"],
				"html_url": run.get("html_url"),
				"status": run.get("status"),
				"created_at": run.get("created_at"),
				"matched_by": "new_run",
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
