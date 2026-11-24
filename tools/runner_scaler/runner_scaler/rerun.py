"""Lost-runner re-run rule (plan goal G6).

A `do-runner` job that failed because its droplet died is re-run once. GitHub's
own `run_attempt` counter is the bound: a job is re-run only at attempt 1, so a
restart of the scaler cannot reset it and a droplet fleet that keeps dying
cannot loop. Evidence is GitHub's lost-runner check-run annotation; a job
without it is a real failure and is left to the pipeline.
"""

from __future__ import annotations

import re
from typing import Any

LOST_RUNNER_RE = re.compile(r"The self-hosted runner: .* lost communication with the server|The runner has received a shutdown signal")

RERUN = "rerun"
GIVEN_UP = "given_up"
NOT_LOST = "not_lost"
NOT_OURS = "not_ours"


def is_ours(job: dict[str, Any], *, label: str) -> bool:
	labels = job.get("labels")
	runner_name = str(job.get("runner_name") or "")
	return isinstance(labels, list) and labels == [label] and runner_name.startswith(f"{label}-")


def is_lost_runner(annotations: Any) -> bool:
	if not isinstance(annotations, list):
		return False
	return any(isinstance(item, dict) and LOST_RUNNER_RE.search(str(item.get("message") or "")) for item in annotations)


def needs_annotations(job: dict[str, Any], *, label: str) -> bool:
	"""True when a completed job could be a lost runner (worth one annotations read)."""
	return is_ours(job, label=label) and job.get("conclusion") == "failure"


def decide(job: dict[str, Any], *, label: str, annotations: Any) -> str:
	if not needs_annotations(job, label=label):
		return NOT_OURS
	if not is_lost_runner(annotations):
		return NOT_LOST
	try:
		attempt = int(job.get("run_attempt") or 0)
	except (TypeError, ValueError):
		attempt = 0
	return RERUN if attempt == 1 else GIVEN_UP
