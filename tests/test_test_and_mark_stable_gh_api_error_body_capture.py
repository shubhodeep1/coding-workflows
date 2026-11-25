#!/usr/bin/env python3
"""Release-gate captures keep gh api's error body out of the value.

On an HTTP error (for example a momentary "API rate limit exceeded"), `gh api`
prints the error body to stdout and exits non-zero. Inside
``V=$(gh api ... 2>/dev/null || echo 0)`` the fallback is appended to that
body, so V held multi-line JSON plus "0". Release gate run 44490333814 then
wrote V to $GITHUB_OUTPUT and the runner killed the
``clarify-rejects-unsolvable-test`` job with "Invalid format".

These tests run the real ``create`` steps against a stub ``gh`` that fails the
runs lookup with a JSON body on stdout, and scan the workflow so the broken
capture shape cannot come back.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import sys
from pathlib import Path

# PyYAML is optional: without it `_step` reads the step's `run: |` block
# from the workflow text, so these tests still run in a container that has
# no PyYAML (instead of the whole module being skipped).
try:
	import yaml
except ImportError:  # pragma: no cover - depends on the environment
	yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import expanded_test_and_mark_stable_text  # noqa: E402

# Mirrors gh's real behaviour on a 403: the error body goes to stdout,
# pretty-printed over several lines, the --jq filter is not applied, exit 1.
STUB_GH = r'''#!/usr/bin/env bash
path="$2"
case "${path}" in
	*/actions/workflows/*/runs*)
		if [ "${STUB_RUNS_MODE:-error}" = "ok" ]; then
			echo "4242"
			exit 0
		fi
		printf '{\n  "message": "API rate limit exceeded for user ID 11442166.",\n  "status": "403"\n}\n'
		echo "gh: API rate limit exceeded (HTTP 403)" >&2
		exit 1
		;;
	*/issues)
		echo "77"
		exit 0
		;;
esac
echo "stub gh: unexpected path ${path}" >&2
exit 1
'''


def _step_from_text(job: str, step_id: str) -> str:
	"""Return a step's literal `run: |` body by scanning the workflow text.

	Jobs sit at 2-space indent, steps at `      - `, step keys at 8 spaces
	and the run body at 10 spaces, as in the shipped workflow.
	"""
	lines = WORKFLOW.read_text(encoding="utf-8").split("\n")
	try:
		start = lines.index(f"  {job}:")
	except ValueError:
		raise AssertionError(f"job {job} not found") from None
	end = next(
		(k for k in range(start + 1, len(lines)) if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[k])),
		len(lines),
	)
	try:
		id_line = lines.index(f"        id: {step_id}", start, end)
	except ValueError:
		raise AssertionError(f"step {job}.{step_id} not found") from None
	k = id_line + 1
	while k < end and not lines[k].startswith("      - ") and lines[k] != "        run: |":
		k += 1
	assert k < end and lines[k] == "        run: |", f"step {job}.{step_id} has no literal run block"
	body = []
	for line in lines[k + 1:end]:
		if line.strip() and not line.startswith(" " * 10):
			break
		body.append(line[10:])
	while body and not body[-1].strip():
		body.pop()
	return "\n".join(body) + "\n"


def _step(job: str, step_id: str) -> str:
	if yaml is None:
		return _step_from_text(job, step_id)
	doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	for step in doc["jobs"][job]["steps"]:
		if step.get("id") == step_id:
			return step["run"]
	raise AssertionError(f"step {job}.{step_id} not found")


def _run_step(body: str, runs_mode: str) -> dict[str, str]:
	with tempfile.TemporaryDirectory() as tmp:
		# The fake gh is a shell function that runs the stub through bash, not
		# an executable on PATH: the pre-push gate container mounts the temp
		# dir noexec, and bash reading a file is not blocked by noexec.
		gh = Path(tmp) / "stub_gh.sh"
		gh.write_text(STUB_GH, encoding="utf-8")
		body = 'gh() { bash "$STUB_GH_SH" "$@"; }\n' + body
		out = Path(tmp) / "github_output"
		out.write_text("", encoding="utf-8")
		env = {
			**os.environ,
			"STUB_GH_SH": str(gh),
			"GITHUB_OUTPUT": str(out),
			"GITHUB_RUN_ID": "1",
			"TEST_REPO": "owner/repo",
			"ALT_EDITOR_MODEL": "m",
			"STUB_RUNS_MODE": runs_mode,
		}
		result = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, timeout=60, check=False)
		assert result.returncode == 0, result.stdout + result.stderr
		lines = out.read_text(encoding="utf-8").splitlines()
	# Every line must be a single-line key=value: a leaked error body would
	# show up as lines without a key, which the runner rejects.
	parsed: dict[str, str] = {}
	for line in lines:
		assert re.match(r"^[A-Za-z0-9_]+=", line), f"malformed GITHUB_OUTPUT line: {line!r}"
		key, _, value = line.partition("=")
		parsed[key] = value
	return parsed


def test_clarify_negative_create_falls_back_to_zero_on_error_body():
	outputs = _run_step(_step("clarify-rejects-unsolvable-test", "create"), "error")
	assert outputs["pre_clarify_run_id"] == "0"
	assert outputs["issue_number"] == "77"


def test_clarify_negative_create_keeps_real_run_id():
	outputs = _run_step(_step("clarify-rejects-unsolvable-test", "create"), "ok")
	assert outputs["pre_clarify_run_id"] == "4242"


def test_alt_model_create_leaves_baseline_empty_on_error_body():
	# Stable keeps #9800's (#9797) rule for the alt-model baselines: a failed
	# read leaves pre_<wf>_run_id empty, never 0, so the capture step skips
	# instead of matching an older, unrelated run with `id > 0`.
	outputs = _run_step(_step("e2e-alt-model-test", "create"), "error")
	for wf in ("clarify", "plan", "implement", "review_autofix"):
		assert outputs[f"pre_{wf}_run_id"] == ""
	assert outputs["issue_number"] == "77"


def test_alt_model_create_keeps_real_run_ids():
	outputs = _run_step(_step("e2e-alt-model-test", "create"), "ok")
	for wf in ("clarify", "plan", "implement", "review_autofix"):
		assert outputs[f"pre_{wf}_run_id"] == "4242"


def test_no_gh_api_capture_puts_its_fallback_inside_the_substitution():
	"""``$(gh api ... || echo X)`` keeps the error body; the fallback must be
	assigned outside: ``V=$(gh api ...) || V=X``. ``|| true`` leaks the body
	the same way, so it is rejected too, except for these captures whose value
	is checked before use: PHASE7_CANCEL_WORKFLOW_STATE (``^[a-z_]+$``),
	LATE_PR_NUMBERS (``^[1-9][0-9]*$``) and PROJECT_BRANCH_PROBE_ERR (stderr
	matched against "HTTP 404")."""
	validated_true_captures = {"PHASE7_CANCEL_WORKFLOW_STATE", "LATE_PR_NUMBERS", "PROJECT_BRANCH_PROBE_ERR"}
	lines = expanded_test_and_mark_stable_text().split("\n")
	offenders = []
	i = 0
	while i < len(lines):
		if "$(gh api" in lines[i] and not lines[i].lstrip().startswith("#"):
			j = i
			while lines[j].rstrip().endswith("\\") and j + 1 < len(lines):
				j += 1
			command = "\n".join(lines[i:j + 1])
			match = re.search(r"\|\|\s*(echo|true|printf)\b[^)]*\)", command)
			assigned = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=", command)
			allowed = match and match.group(1) == "true" and assigned and assigned.group(1) in validated_true_captures
			if match and not allowed:
				offenders.append(i + 1)
			i = j + 1
		else:
			i += 1
	assert offenders == [], f"gh api captures with an in-substitution fallback at lines {offenders}"
