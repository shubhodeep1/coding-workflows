#!/usr/bin/env python3
"""Contract test: codex-agent logs free disk space around free-disk-space.

Source: analysis/workflow-optimization-2026-09-30.md (Speed Optimizations #2).
The review "Free disk space" step took 96-498 s in sampled runs. Before anyone
considers skipping it, the job must log free space before AND after cleanup
so the reclaimed headroom can be computed from exact byte counts.

This test pins, in the `codex-agent` job of .github/workflows/review_autofix.yml:

1. "Show disk usage (pre free-disk-space)" is the step immediately before
   "Free disk space", and "Show disk usage (post free-disk-space)" is the step
   immediately after it.
2. Both log `df -B1 --output=avail,size / || true`; the pre step can never
   fail the job, and neither step interpolates `${{ }}` expressions.
3. The free-disk-space action's SHA pin and `with:` knobs are unchanged
   (this change is logging only; a skip threshold is out of scope).
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"

PRE_STEP = "Show disk usage (pre free-disk-space)"
POST_STEP = "Show disk usage (post free-disk-space)"
FREE_STEP = "Free disk space"
BYTES_CMD = "df -B1 --output=avail,size / || true"
PINNED_USES = "jlumbroso/free-disk-space@54081f138730dfa15788a46383842cd2f914a1be"
EXPECTED_WITH = {
	"tool-cache": True,
	"android": True,
	"dotnet": True,
	"haskell": True,
	"large-packages": False,
	"docker-images": True,
	"swap-storage": True,
}


# Stdlib-only reader for the block-style YAML this test inspects: PyYAML is
# not installed in the credential-free pre-push test container, so the test
# must not import it. It reads `jobs:` -> `<job>:` -> `steps:` -> list items and,
# per step, the scalar keys, the `with:` mapping, and `run: |` block scalars.
_JOB_KEY_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$")
_STEP_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):(?:\s+(.*))?$")
_TOP_LEVEL_RE = re.compile(r"^[^\s#]")


def _indent(line: str) -> int:
	return len(line) - len(line.lstrip(" "))


def _scalar(raw: str):
	value = raw.strip()
	if value[:1] in ("'", '"'):
		quote = value[0]
		end = value.find(quote, 1)
		return value[1:end] if end > 0 else value[1:]
	value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
	if value == "true":
		return True
	if value == "false":
		return False
	return value


def _parse_steps(lines: list[str], step_indent: int) -> list[dict]:
	key_indent = step_indent + 2
	steps: list[dict] = []
	current: dict | None = None
	i = 0
	while i < len(lines):
		line = lines[i]
		stripped = line.strip()
		if not stripped or stripped.startswith("#"):
			i += 1
			continue
		ind = _indent(line)
		if ind == step_indent and stripped.startswith("- "):
			current = {}
			steps.append(current)
			body = stripped[2:].strip()
		elif ind == key_indent and current is not None:
			body = stripped
		else:
			i += 1
			continue
		match = _STEP_KEY_RE.match(body)
		i += 1
		if not match:
			continue
		key, value = match.group(1), (match.group(2) or "").strip()
		if value.startswith("|") or value.startswith(">"):
			block: list[str] = []
			while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > key_indent):
				block.append(lines[i])
				i += 1
			while block and not block[-1].strip():
				block.pop()
			body_indent = min((_indent(b) for b in block if b.strip()), default=0)
			current[key] = "".join(b[body_indent:] + "\n" for b in block)
		elif value and not value.startswith("#"):
			current[key] = _scalar(value)
		else:
			mapping: dict = {}
			while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > key_indent):
				sub = lines[i].strip()
				i += 1
				if not sub or sub.startswith("#"):
					continue
				sub_match = _STEP_KEY_RE.match(sub)
				if sub_match:
					mapping[sub_match.group(1)] = _scalar(sub_match.group(2) or "")
			current[key] = mapping
	return steps


def _workflow_doc() -> dict:
	lines = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8").splitlines()
	start = next((n for n, line in enumerate(lines) if line.rstrip() == "jobs:"), None)
	assert start is not None, "jobs: block not found in review_autofix.yml"
	job_bounds: list[tuple[str, int]] = []
	end = len(lines)
	for n in range(start + 1, len(lines)):
		if _TOP_LEVEL_RE.match(lines[n]):
			end = n
			break
		match = _JOB_KEY_RE.match(lines[n])
		if match:
			job_bounds.append((match.group(1), n))
	jobs: dict = {}
	for idx, (job_id, job_start) in enumerate(job_bounds):
		job_end = job_bounds[idx + 1][1] if idx + 1 < len(job_bounds) else end
		job_lines = lines[job_start + 1:job_end]
		steps_at = next(
			(n for n, line in enumerate(job_lines) if line.rstrip() == "    steps:"), None
		)
		steps: list[dict] = []
		if steps_at is not None:
			steps_lines: list[str] = []
			for line in job_lines[steps_at + 1:]:
				if line.strip() and not line.strip().startswith("#") and _indent(line) <= 4:
					break
				steps_lines.append(line)
			step_indent = min(
				(_indent(step_line) for step_line in steps_lines if step_line.strip().startswith("- ")),
				default=6,
			)
			steps = _parse_steps(steps_lines, step_indent)
		jobs[job_id] = {"steps": steps}
	return {"jobs": jobs}


def _codex_agent_steps() -> list[dict]:
	jobs = _workflow_doc().get("jobs") or {}
	job = jobs.get("codex-agent")
	assert isinstance(job, dict), "codex-agent job not found in review_autofix.yml"
	return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _step_named(name: str) -> dict:
	matches = [s for s in _codex_agent_steps() if s.get("name") == name]
	assert len(matches) == 1, (
		f"expected exactly one codex-agent step named {name!r}, found {len(matches)}"
	)
	return matches[0]


def _run_lines(step: dict) -> list[str]:
	run = step.get("run")
	assert isinstance(run, str), f"step {step.get('name')!r} has no run: body"
	return [line.strip() for line in run.splitlines() if line.strip()]


def test_pre_and_post_steps_bracket_free_disk_space() -> None:
	steps = _codex_agent_steps()
	names = [s.get("name") for s in steps]
	for name in (FREE_STEP, PRE_STEP, POST_STEP):
		assert names.count(name) == 1, (
			f"expected exactly one codex-agent step named {name!r}, "
			f"found {names.count(name)}"
		)
	idx = names.index(FREE_STEP)
	assert idx > 0 and names[idx - 1] == PRE_STEP, (
		f"{PRE_STEP!r} must be the step immediately before {FREE_STEP!r}; "
		f"found {names[idx - 1] if idx > 0 else None!r}"
	)
	assert idx + 1 < len(names) and names[idx + 1] == POST_STEP, (
		f"{POST_STEP!r} must be the step immediately after {FREE_STEP!r}; "
		f"found {names[idx + 1] if idx + 1 < len(names) else None!r}"
	)


def test_pre_step_logs_bytes_and_never_fails() -> None:
	step = _step_named(PRE_STEP)
	run = step["run"]
	lines = _run_lines(step)
	assert any(line.startswith("df -h /") for line in lines), (
		f"{PRE_STEP!r} must log `df -h /`; run lines: {lines}"
	)
	assert BYTES_CMD in lines, f"{PRE_STEP!r} must run {BYTES_CMD!r}; run lines: {lines}"
	for line in lines:
		assert line.endswith("|| true"), (
			f"{PRE_STEP!r} must never fail the job; line lacks `|| true`: {line!r}"
		)
	assert "if" not in step, f"{PRE_STEP!r} must not carry an if: condition"
	assert "uses" not in step, f"{PRE_STEP!r} must be a plain run: step"
	assert "${{" not in run, f"{PRE_STEP!r} run: body must not use ${{{{ }}}} expressions"


def test_post_step_keeps_df_h_and_adds_bytes() -> None:
	step = _step_named(POST_STEP)
	run = step["run"]
	lines = _run_lines(step)
	assert "df -h /" in lines, (
		f"{POST_STEP!r} must keep its original `df -h /` line unchanged; run lines: {lines}"
	)
	assert BYTES_CMD in lines, f"{POST_STEP!r} must run {BYTES_CMD!r}; run lines: {lines}"
	assert "${{" not in run, f"{POST_STEP!r} run: body must not use ${{{{ }}}} expressions"
	assert "if" not in step, f"{POST_STEP!r} must not carry an if: condition"


def test_free_disk_space_pin_and_knobs_unchanged() -> None:
	step = _step_named(FREE_STEP)
	assert step.get("uses") == PINNED_USES, (
		f"{FREE_STEP!r} must stay pinned to {PINNED_USES!r}; found {step.get('uses')!r}"
	)
	assert step.get("with") == EXPECTED_WITH, (
		f"{FREE_STEP!r} with: knobs changed; expected {EXPECTED_WITH}, "
		f"found {step.get('with')}"
	)
	assert "if" not in step, f"{FREE_STEP!r} must stay unconditional (skip threshold is out of scope)"


def test_pre_step_only_in_codex_agent() -> None:
	jobs = _workflow_doc().get("jobs") or {}
	offenders = []
	for job_id, job in jobs.items():
		if job_id == "codex-agent" or not isinstance(job, dict):
			continue
		for step in job.get("steps") or []:
			if isinstance(step, dict) and step.get("name") == PRE_STEP:
				offenders.append(job_id)
	assert not offenders, f"{PRE_STEP!r} must only appear in codex-agent; also in: {offenders}"


def main() -> int:
	test_pre_and_post_steps_bracket_free_disk_space()
	test_pre_step_logs_bytes_and_never_fails()
	test_post_step_keeps_df_h_and_adds_bytes()
	test_free_disk_space_pin_and_knobs_unchanged()
	test_pre_step_only_in_codex_agent()
	print("OK: review_autofix codex-agent disk headroom logging contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
