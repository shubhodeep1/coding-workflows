#!/usr/bin/env python3
"""Contract test: codex-agent force-tick steps must bind REPOSITORY.

Regression guard for the `REPOSITORY: unbound variable` crash reported from
consumer run 27995931397 (shubhodeep1/fun-token-multi-chain). Two steps in
the `codex-agent` job of .github/workflows/review_autofix.yml —

  * "Force orchestrate poll after review-blocked exhaustion"
  * "Force orchestrate poll after workflow failure review-blocked label"

— reference the shell variable ${REPOSITORY} in their run: block under
`set -euo pipefail` (passing it to orchestrate_force_tick.sh --repo). The
`codex-agent` job declares no job-level REPOSITORY and the workflow never
exports REPOSITORY to $GITHUB_ENV, so REPOSITORY only ever resolves when a
step declares it in its own env: block (the established per-step pattern,
e.g. the rb_judge step and the two review-blocked-label steps). The two
force-tick steps originally omitted it, so under `set -u` the reference
expanded to an unbound variable and aborted the step with exit 1 — crashing
the orchestrator force tick and converting a clean ai:review-blocked handoff
into a spurious "PR autofix failed" alert.

This test pins two things:

1. The two named force-tick steps declare REPOSITORY: ${{ github.repository }}.
2. The general invariant: EVERY step in the codex-agent job that references
   ${REPOSITORY} in its run: block has REPOSITORY bound — via the job-level
   env, its own step env, or a preceding step that exports REPOSITORY to
   $GITHUB_ENV. This catches the next force-tick / notification step that
   forgets the per-step declaration, not just the two known offenders.

It does NOT execute the run: blocks; it asserts on the parsed workflow
structure, which is the right granularity for a "did anyone reintroduce the
unbound-variable footgun" guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"

# Matches the shell variable REPOSITORY (braced or bare) without matching
# longer tokens that merely share the suffix, e.g. ${GITHUB_REPOSITORY},
# ${CURRENT_REPOSITORY}, or ${GITHUB_REPOSITORY_OWNER}. A leading word
# character before REPOSITORY disqualifies the match; a trailing word
# character (REPOSITORY_ID) disqualifies it too.
_REPOSITORY_REF = re.compile(r"(?<![A-Za-z0-9_])\$\{?REPOSITORY(?![A-Za-z0-9_])")

# Detects a step that publishes REPOSITORY to $GITHUB_ENV (so a *later* step
# inherits it without a per-step env: declaration). The workflow does not do
# this today, but modelling it keeps the invariant correct rather than brittle
# if a maintainer later hoists REPOSITORY into $GITHUB_ENV.
_REPOSITORY_GITHUB_ENV_EXPORT = re.compile(
	r"REPOSITORY=.*GITHUB_ENV", re.DOTALL
)


def _workflow_text() -> str:
	return REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")


def _workflow_doc() -> dict:
	return yaml.safe_load(_workflow_text())


def _codex_agent_job() -> dict:
	jobs = (_workflow_doc().get("jobs") or {})
	job = jobs.get("codex-agent")
	assert isinstance(job, dict), (
		"codex-agent job not found in review_autofix.yml — the job id is "
		"referenced by internal-review.yml / required-checks rules and must "
		"not be renamed (CLAUDE.md §6)"
	)
	return job


def _step_run(step: dict) -> str:
	run = step.get("run")
	return run if isinstance(run, str) else ""


def _step_env(step: dict) -> dict:
	env = step.get("env")
	return env if isinstance(env, dict) else {}


def test_named_force_tick_steps_bind_repository() -> None:
	"""The two force-tick steps must declare REPOSITORY in their env block."""
	job = _codex_agent_job()
	steps = job.get("steps") or []
	want = {
		"Force orchestrate poll after review-blocked exhaustion",
		"Force orchestrate poll after workflow failure review-blocked label",
	}
	seen: set[str] = set()
	for step in steps:
		if not isinstance(step, dict):
			continue
		name = step.get("name")
		if name not in want:
			continue
		seen.add(name)
		run = _step_run(step)
		assert _REPOSITORY_REF.search(run), (
			f"step {name!r} no longer references ${{REPOSITORY}} in its run "
			f"block — if the force tick was refactored, update this guard"
		)
		env = _step_env(step)
		assert "REPOSITORY" in env, (
			f"step {name!r} references ${{REPOSITORY}} under `set -u` but "
			f"omits REPOSITORY from its env: block. The codex-agent job has "
			f"no job-level REPOSITORY and never exports it to $GITHUB_ENV, so "
			f"this aborts the force tick with 'REPOSITORY: unbound variable' "
			f"(consumer run 27995931397). Declared env keys: "
			f"{sorted(env.keys())}"
		)
		normalized_repository_env = re.sub(r"\s+", "", str(env["REPOSITORY"]))
		assert "${{github.repository}}" in normalized_repository_env, (
			f"step {name!r} must bind REPOSITORY to ${{{{ github.repository }}}} "
			f"(the value every other REPOSITORY-using step in this job uses; in "
			f"a reusable workflow it resolves to the calling consumer repo, "
			f"which is the correct force-tick target). Found: {env['REPOSITORY']!r}"
		)
	missing = want - seen
	assert not missing, (
		f"expected force-tick step(s) not found in codex-agent job: "
		f"{sorted(missing)} — if intentionally renamed, update this guard"
	)


def test_every_repository_ref_in_codex_agent_is_bound() -> None:
	"""No codex-agent step may reference ${REPOSITORY} without binding it.

	General invariant behind the two named offenders: a step's ${REPOSITORY}
	reference is satisfiable only if REPOSITORY is available from the job-level
	env, the step's own env, or a $GITHUB_ENV export from an earlier step.
	"""
	job = _codex_agent_job()
	job_env = job.get("env") if isinstance(job.get("env"), dict) else {}
	job_level_repository = "REPOSITORY" in (job_env or {})

	steps = [s for s in (job.get("steps") or []) if isinstance(s, dict)]

	# Indices of steps that publish REPOSITORY to $GITHUB_ENV; a later step
	# inherits it. (None today — kept for forward-compatibility.)
	exporter_indices = [
		idx
		for idx, step in enumerate(steps)
		if _REPOSITORY_GITHUB_ENV_EXPORT.search(_step_run(step))
	]

	offenders: list[str] = []
	for idx, step in enumerate(steps):
		run = _step_run(step)
		if not _REPOSITORY_REF.search(run):
			continue
		if job_level_repository:
			continue
		if "REPOSITORY" in _step_env(step):
			continue
		# Satisfied only by an export from a strictly-earlier step.
		if any(e_idx < idx for e_idx in exporter_indices):
			continue
		offenders.append(step.get("name") or f"<unnamed step #{idx}>")

	assert not offenders, (
		"codex-agent step(s) reference ${REPOSITORY} in run: under `set -u` "
		"but never bind it (no job-level REPOSITORY, no step env REPOSITORY, "
		"no prior $GITHUB_ENV export). Each will abort with 'REPOSITORY: "
		f"unbound variable' at runtime: {offenders}. Add "
		"`REPOSITORY: ${{ github.repository }}` to the step's env: block."
	)


def main() -> int:
	test_named_force_tick_steps_bind_repository()
	test_every_repository_ref_in_codex_agent_is_bound()
	print("OK: review_autofix codex-agent force-tick REPOSITORY-env contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
