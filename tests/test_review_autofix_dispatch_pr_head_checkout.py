"""Regression tests: dispatched review runs check out the PR head (issue #5824).

Run 36794195824 (PR #5097) was a ``workflow_dispatch`` of
``review_autofix.yml`` by the hourly sweep. ``codex-agent`` → "Checkout repo"
checked out ``github.event.pull_request.head.sha || github.sha``; a dispatch
has no ``pull_request`` payload, so ``GITHUB_WORKSPACE`` held the default
branch. The reviewers read files from ``GITHUB_WORKSPACE``
(``scripts/review_run_reviewers.sh``: ``reviewer_opencode_workspace``) while
their git commands showed the PR's diff, so all 30 ledger entries reported the
PR's changes as missing at HEAD.

The gate now exports ``review_checkout_sha`` (the same-repository PR head it
read from its existing ``/pulls/<n>`` fetch) and the checkout prefers it over
``github.sha``. These tests pin:
  1. the wiring (gate jq field, gate output, checkout expression, job needs);
  2. the gate fragment's decision, run under bash for each event shape;
  3. end to end on a scratch repo: the reviewer workspace of a dispatched run
     contains a file the PR head adds.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
REVIEWERS_SCRIPT = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
AGENTS_MD = REPO_ROOT / "agents.md"

REPOSITORY = "shubhodeep1/coding-workflows"
HEAD = "a645b00f3c3d0d6c1e2f8b9a7c6d5e4f3a2b1c0d"
EVENT_SHA = "36aab34e1f2d3c4b5a69788776655443322110ff"
CHECKOUT_EXPR = (
	"${{ github.event.pull_request.head.sha || needs.gate.outputs.review_checkout_sha || github.sha }}"
)


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _workflow_yaml() -> dict:
	return yaml.safe_load(_workflow_text())


def _gate_block() -> str:
	lines = _workflow_text().splitlines()
	needle = "- name: Evaluate review gate"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		step_indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:"):
				indent = len(candidate) - len(candidate.lstrip(" "))
				if indent == step_indent:
					end = j
					break
		return "\n".join(lines[idx:end])
	raise AssertionError("Evaluate review gate step not found")


def _checkout_fragment() -> str:
	"""The gate's review_checkout_sha block, from its init to its output line."""
	lines = _gate_block().splitlines()
	start = next((i for i, line in enumerate(lines) if line.strip() == 'review_checkout_sha_gate=""'), -1)
	assert start >= 0, "review_checkout_sha_gate initialisation not found in the gate step"
	end = next(
		(
			i
			for i in range(start, len(lines))
			if lines[i].strip() == 'echo "review_checkout_sha=${review_checkout_sha_gate}" >> "${GITHUB_OUTPUT}"'
		),
		-1,
	)
	assert end > start, "review_checkout_sha output line not found after its initialisation"
	return textwrap.dedent("\n".join(lines[start : end + 1]))


def _run_fragment(tmp_path: Path, *, pr_number: str, head_sha: str, head_repo: str, event_name: str) -> tuple[str, str]:
	output_file = tmp_path / "github_output"
	output_file.write_text("", encoding="utf-8")
	script = "set -euo pipefail\n" + _checkout_fragment() + "\n"
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"PR_NUMBER": pr_number,
		"pr_head_sha_gate": head_sha,
		"pr_head_repo_gate": head_repo,
		"REPOSITORY": REPOSITORY,
		"EVENT_NAME": event_name,
		"GITHUB_OUTPUT": str(output_file),
	}
	result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, check=True)
	outputs = dict(
		line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line
	)
	return outputs.get("review_checkout_sha", "<missing>"), result.stdout


def _resolve_checkout_ref(*, event_head_sha: str, gate_checkout_sha: str, github_sha: str) -> str:
	"""Evaluate the checkout expression the way GitHub's `||` does (first non-empty operand)."""
	match = re.fullmatch(r"\$\{\{ (.+) \}\}", CHECKOUT_EXPR)
	assert match is not None
	values = {
		"github.event.pull_request.head.sha": event_head_sha,
		"needs.gate.outputs.review_checkout_sha": gate_checkout_sha,
		"github.sha": github_sha,
	}
	operands = [part.strip() for part in match.group(1).split("||")]
	assert operands == list(values), operands
	return next((values[name] for name in operands if values[name]), "")


def _codex_agent_checkout_step() -> dict:
	steps = _workflow_yaml()["jobs"]["codex-agent"]["steps"]
	return next(step for step in steps if step.get("name") == "Checkout repo")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_checkout_prefers_gate_pr_head_before_github_sha():
	step = _codex_agent_checkout_step()
	assert step["uses"].startswith("actions/checkout@")
	assert step["with"]["ref"] == CHECKOUT_EXPR


def test_codex_agent_needs_gate_and_gate_exports_review_checkout_sha():
	data = _workflow_yaml()
	needs = data["jobs"]["codex-agent"]["needs"]
	needs_list = [needs] if isinstance(needs, str) else list(needs)
	assert "gate" in needs_list
	outputs = data["jobs"]["gate"]["outputs"]
	assert outputs["review_checkout_sha"] == "${{ steps.evaluate.outputs.review_checkout_sha }}"
	# head_sha keeps its meaning for the deterministic-skip and Claude-fixer consumers.
	assert outputs["head_sha"] == "${{ steps.evaluate.outputs.head_sha }}"


def test_head_repo_rides_the_existing_pulls_fetch():
	gate = _gate_block()
	# One /pulls/{n} fetch only (CLAUDE.md §15): the head repository is a field of it.
	assert gate.count('gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"') == 1
	assert 'head_repo: (.head.repo.full_name // "")' in gate
	assert 'pr_head_repo_gate="$(printf \'%s\' "${_pr_gate}" | jq -r \'.head_repo // ""\'' in gate


def test_pulls_jq_projection_extracts_head_repo():
	gate = _gate_block()
	match = re.search(r"--jq '(\{state: .*?\})'", gate)
	assert match is not None, "gate /pulls --jq projection not found"
	payload = {
		"state": "open",
		"merged": False,
		"head": {"ref": "claude/x", "sha": HEAD, "repo": {"full_name": REPOSITORY}},
		"labels": [],
		"additions": 1,
		"deletions": 0,
		"changed_files": 1,
		"mergeable": True,
		"mergeable_state": "clean",
		"title": "t",
		"body": "b",
	}
	result = subprocess.run(
		["jq", "-c", match.group(1)], input=json.dumps(payload), text=True, capture_output=True, check=True
	)
	projected = json.loads(result.stdout)
	assert projected["head_repo"] == REPOSITORY
	assert projected["head_sha"] == HEAD
	payload["head"]["repo"] = None  # a deleted fork
	result = subprocess.run(
		["jq", "-c", match.group(1)], input=json.dumps(payload), text=True, capture_output=True, check=True
	)
	assert json.loads(result.stdout)["head_repo"] == ""


def test_reviewers_read_files_from_github_workspace():
	"""The checkout above is what the reviewers read; keep the two pinned together."""
	text = REVIEWERS_SCRIPT.read_text(encoding="utf-8")
	assert 'local reviewer_opencode_workspace="${GITHUB_WORKSPACE:-$(pwd)}"' in text


def test_agents_md_readers_use_checked_out_workspace():
	"""Every reader the agents.md "Review checkout on dispatched runs" section names
	resolves files from GITHUB_WORKSPACE in the codex-agent job (PR #5857 review round 2)."""
	agents_text = AGENTS_MD.read_text(encoding="utf-8")
	assert (
		"The reviewer panel, the summariser, the slop scan,\n"
		"  and the reviewer file-context helpers read files from `GITHUB_WORKSPACE`"
	) in agents_text
	steps = _workflow_yaml()["jobs"]["codex-agent"]["steps"]
	step_names = [step.get("name") for step in steps]
	assert step_names.index("Checkout repo") < step_names.index("Run reviewer models")
	# Reviewer file-context helpers: targeted file context and the uninteresting-file filter.
	reviewers_text = REVIEWERS_SCRIPT.read_text(encoding="utf-8")
	assert 'python3 "${TARGETED_FILE_CONTEXT_SCRIPT}"' in reviewers_text
	assert reviewers_text.count('--repo-root "${GITHUB_WORKSPACE:-$(pwd)}"') >= 3
	# Summariser, launched by review_run_reviewers.sh inside "Run reviewer models".
	assert 'SUMMARISER_SCRIPT="${SUPPORT_SCRIPTS_DIR:-scripts}/summarize_reviewer_consensus.sh"' in reviewers_text
	summariser_text = (REPO_ROOT / "scripts" / "summarize_reviewer_consensus.sh").read_text(encoding="utf-8")
	assert 'summariser_workspace="${GITHUB_WORKSPACE:-$(pwd)}"' in summariser_text
	assert '--project-path "${summariser_workspace}"' in summariser_text
	# Slop scan, its own step after the checkout in the same job.
	slop_step = next(step for step in steps if step.get("name") == "Collect local slop-scan findings")
	assert step_names.index("Checkout repo") < step_names.index("Collect local slop-scan findings")
	assert '--repo-root "${GITHUB_WORKSPACE}"' in slop_step["run"]


def test_review_checkout_log_prefix_is_registered_in_agents_md():
	"""The gate's new log prefix is in both contractual inventories (PR #5857 review round 1)."""
	assert "AUTOFIX_GATE_REVIEW_CHECKOUT pr=" in _workflow_text()
	agents_text = AGENTS_MD.read_text(encoding="utf-8")
	assert "\n- `AUTOFIX_GATE_REVIEW_CHECKOUT`\n" in agents_text
	assert "\nLOG_PREFIX.name=AUTOFIX_GATE_REVIEW_CHECKOUT\n" in agents_text


# ---------------------------------------------------------------------------
# Gate decision, run under bash
# ---------------------------------------------------------------------------


def test_dispatch_same_repo_head_checks_out_pr_head(tmp_path):
	sha, stdout = _run_fragment(
		tmp_path, pr_number="5097", head_sha=HEAD, head_repo=REPOSITORY, event_name="workflow_dispatch"
	)
	assert sha == HEAD
	assert "AUTOFIX_GATE_REVIEW_CHECKOUT pr=5097 event=workflow_dispatch" in stdout
	assert "checkout=pr_head" in stdout
	ref = _resolve_checkout_ref(event_head_sha="", gate_checkout_sha=sha, github_sha=EVENT_SHA)
	assert ref == HEAD


def test_pull_request_event_keeps_event_head(tmp_path):
	sha, _ = _run_fragment(tmp_path, pr_number="5097", head_sha=HEAD, head_repo=REPOSITORY, event_name="pull_request")
	event_head = "b" * 40
	assert _resolve_checkout_ref(event_head_sha=event_head, gate_checkout_sha=sha, github_sha=EVENT_SHA) == event_head


def test_dispatch_cross_repo_head_keeps_event_sha_and_warns(tmp_path):
	sha, stdout = _run_fragment(
		tmp_path, pr_number="5097", head_sha=HEAD, head_repo="someone/fork", event_name="workflow_dispatch"
	)
	assert sha == ""
	assert "checkout=event_sha reason=cross_repo_head" in stdout
	assert "::warning::PR #5097 head is in someone/fork" in stdout
	assert "not the PR head, so it skips the review." in stdout
	assert _resolve_checkout_ref(event_head_sha="", gate_checkout_sha=sha, github_sha=EVENT_SHA) == EVENT_SHA


def test_cross_repo_head_on_pull_request_event_does_not_warn(tmp_path):
	sha, stdout = _run_fragment(tmp_path, pr_number="5097", head_sha=HEAD, head_repo="someone/fork", event_name="pull_request")
	assert sha == ""
	assert "::warning::" not in stdout


def test_missing_head_repo_is_not_same_repo(tmp_path):
	sha, stdout = _run_fragment(tmp_path, pr_number="5097", head_sha=HEAD, head_repo="", event_name="workflow_dispatch")
	assert sha == ""
	assert "head_repo=unknown" in stdout


def test_no_pr_push_path_uses_github_sha(tmp_path):
	sha, stdout = _run_fragment(tmp_path, pr_number="", head_sha="", head_repo="", event_name="push")
	assert sha == ""
	assert stdout == ""
	assert _resolve_checkout_ref(event_head_sha="", gate_checkout_sha=sha, github_sha=EVENT_SHA) == EVENT_SHA


def test_invalid_head_sha_is_not_used(tmp_path):
	sha, _ = _run_fragment(tmp_path, pr_number="5097", head_sha="main", head_repo=REPOSITORY, event_name="workflow_dispatch")
	assert sha == ""


# ---------------------------------------------------------------------------
# Head moved after the workspace checkout (PR #5857 review round 3)
# ---------------------------------------------------------------------------


def _codex_agent_step(name: str) -> dict:
	steps = _workflow_yaml()["jobs"]["codex-agent"]["steps"]
	return next(step for step in steps if step.get("name") == name)


def _head_moved_fragment() -> str:
	"""The "Checkout PR head branch" comparison block, from its `if` to its `fi`."""
	lines = _codex_agent_step("Checkout PR head branch")["run"].splitlines()
	start = next(
		(i for i, line in enumerate(lines) if line.strip() == 'if [ "${INITIAL_HEAD_SHA}" != "${review_workspace_sha}" ]; then'),
		-1,
	)
	assert start >= 0, "workspace/head comparison not found in Checkout PR head branch"
	indent = len(lines[start]) - len(lines[start].lstrip(" "))
	end = next(
		(i for i in range(start + 1, len(lines)) if lines[i].strip() == "fi" and len(lines[i]) - len(lines[i].lstrip(" ")) == indent),
		-1,
	)
	assert end > start, "closing fi of the workspace/head comparison not found"
	return textwrap.dedent("\n".join(lines[start : end + 1]))


def _run_head_moved_fragment(tmp_path: Path, *, workspace_sha: str, head_sha: str) -> tuple[str, str]:
	env_file = tmp_path / "github_env"
	env_file.write_text("", encoding="utf-8")
	script = "set -euo pipefail\n" + _head_moved_fragment() + "\n"
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"PR_NUMBER": "5857",
		"HEAD_REF": "claude/feature",
		"review_workspace_sha": workspace_sha,
		"INITIAL_HEAD_SHA": head_sha,
		"GITHUB_ENV": str(env_file),
	}
	result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, check=True)
	return env_file.read_text(encoding="utf-8"), result.stdout


def test_checkout_pr_head_branch_records_workspace_sha_before_reset():
	run = _codex_agent_step("Checkout PR head branch")["run"]
	capture = run.index('review_workspace_sha="${INITIAL_HEAD_SHA}"')
	first_head = run.index('INITIAL_HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"')
	fetch = run.index('git fetch --no-tags --prune origin "+refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF}"')
	reset = run.index('git reset --hard "refs/remotes/origin/${HEAD_REF}"')
	recapture = run.index('INITIAL_HEAD_SHA="$(git rev-parse HEAD)"')
	compare = run.index('if [ "${INITIAL_HEAD_SHA}" != "${review_workspace_sha}" ]; then')
	assert first_head < capture < fetch < reset < recapture < compare


def test_head_moved_skip_gates_reviewers_handoff_and_auto_merge():
	"""AUTOFIX_STALE_BASE_SKIP is what keeps the run from reviewing or merging the moved head."""
	for name in (
		"Run reviewer models",
		"Hand review round to Claude session (Claude-fixer mode)",
		"Enable auto-merge on PR",
	):
		assert "env.AUTOFIX_STALE_BASE_SKIP != 'true'" in str(_codex_agent_step(name)["if"]), name


def test_unmoved_head_keeps_running(tmp_path):
	env_text, stdout = _run_head_moved_fragment(tmp_path, workspace_sha=HEAD, head_sha=HEAD)
	assert env_text == ""
	assert stdout == ""


def test_moved_head_soft_exits(tmp_path):
	moved = "c" * 40
	env_text, stdout = _run_head_moved_fragment(tmp_path, workspace_sha=HEAD, head_sha=moved)
	assert env_text == "AUTOFIX_STALE_BASE_SKIP=true\n"
	assert (
		f"AUTOFIX_REVIEW_WORKSPACE_HEAD_MOVED pr=5857 workspace_sha={HEAD} head_sha={moved} "
		"target_branch=claude/feature action=soft_exit"
	) in stdout
	assert "::warning::claude/feature moved from" in stdout


def test_unknown_workspace_sha_soft_exits(tmp_path):
	env_text, stdout = _run_head_moved_fragment(tmp_path, workspace_sha="", head_sha=HEAD)
	assert env_text == "AUTOFIX_STALE_BASE_SKIP=true\n"
	assert "workspace_sha=unknown" in stdout


def test_head_moved_log_prefix_is_registered_in_agents_md():
	assert "AUTOFIX_REVIEW_WORKSPACE_HEAD_MOVED pr=" in _workflow_text()
	agents_text = AGENTS_MD.read_text(encoding="utf-8")
	assert "\n- `AUTOFIX_REVIEW_WORKSPACE_HEAD_MOVED`\n" in agents_text
	assert "\nLOG_PREFIX.name=AUTOFIX_REVIEW_WORKSPACE_HEAD_MOVED\n" in agents_text


# ---------------------------------------------------------------------------
# Head moved before the PR metadata was read (PR #5857 review round 4)
# ---------------------------------------------------------------------------

FORK_REPOSITORY = "someone/coding-workflows"
METADATA_CHECK_START = 'review_metadata_head_sha=""'


def _metadata_check_fragment() -> str:
	"""The metadata-head comparison, from its variable to the `fi` of its second `if`."""
	lines = _codex_agent_step("Checkout PR head branch")["run"].splitlines()
	start = next((i for i, line in enumerate(lines) if line.strip() == METADATA_CHECK_START), -1)
	assert start >= 0, "metadata-head comparison not found in Checkout PR head branch"
	indent = len(lines[start]) - len(lines[start].lstrip(" "))
	closers = [
		i for i in range(start + 1, len(lines))
		if lines[i].strip() == "fi" and len(lines[i]) - len(lines[i].lstrip(" ")) == indent
	]
	assert len(closers) >= 2, "closing fi of the metadata-head comparison not found"
	return textwrap.dedent("\n".join(lines[start : closers[1] + 1]))


def _run_metadata_check(
	tmp_path: Path,
	*,
	workspace_sha: str,
	metadata_head_sha: str | None,
	event_head_sha: str,
	head_repo: str,
	pr_number: str = "5857",
) -> tuple[str, str]:
	env_file = tmp_path / "github_env"
	env_file.write_text("", encoding="utf-8")
	payload_file = tmp_path / "pr_payload.json"
	if metadata_head_sha is not None:
		payload_file.write_text(json.dumps({"head": {"sha": metadata_head_sha}}), encoding="utf-8")
	fragment = (
		_metadata_check_fragment()
		.replace("${{ github.event.pull_request.head.sha }}", event_head_sha)
		.replace("${{ github.repository }}", REPOSITORY)
	)
	assert "${{" not in fragment
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"PR_NUMBER": pr_number,
		"PR_PAYLOAD_FILE": str(payload_file),
		"HEAD_REF": "feature",
		"HEAD_REPO": head_repo,
		"review_workspace_sha": workspace_sha,
		"GITHUB_ENV": str(env_file),
	}
	result = subprocess.run(
		["bash", "-c", "set -euo pipefail\n" + fragment + "\n"],
		env=env,
		text=True,
		capture_output=True,
		check=True,
	)
	return env_file.read_text(encoding="utf-8"), result.stdout


def test_metadata_head_check_runs_before_every_non_push_exit():
	run = _codex_agent_step("Checkout PR head branch")["run"]
	capture = run.index('review_workspace_sha="${INITIAL_HEAD_SHA}"')
	check = run.index(METADATA_CHECK_START)
	first_exit = re.search(r"^\s*exit \d", run, re.MULTILINE).start()
	fork_exit = run.index('echo "PR head repo (${HEAD_REPO}) is not writable from this workflow."')
	assert capture < check < first_exit < fork_exit


def test_fork_pull_request_with_moved_head_soft_exits(tmp_path):
	moved = "c" * 40
	env_text, stdout = _run_metadata_check(
		tmp_path, workspace_sha=HEAD, metadata_head_sha=moved, event_head_sha=HEAD, head_repo=FORK_REPOSITORY
	)
	assert env_text == "AUTOFIX_STALE_BASE_SKIP=true\n"
	assert (
		f"AUTOFIX_REVIEW_WORKSPACE_HEAD_MOVED pr=5857 workspace_sha={HEAD} head_sha={moved} "
		"target_branch=feature source=pr_metadata action=soft_exit"
	) in stdout
	assert "::warning::PR #5857 head moved from" in stdout


def test_fork_pull_request_with_unmoved_head_keeps_running(tmp_path):
	env_text, stdout = _run_metadata_check(
		tmp_path, workspace_sha=HEAD, metadata_head_sha=HEAD, event_head_sha=HEAD, head_repo=FORK_REPOSITORY
	)
	assert env_text == ""
	assert stdout == ""


def test_same_repo_dispatch_with_moved_head_soft_exits(tmp_path):
	moved = "c" * 40
	env_text, stdout = _run_metadata_check(
		tmp_path, workspace_sha=HEAD, metadata_head_sha=moved, event_head_sha="", head_repo=REPOSITORY
	)
	assert env_text == "AUTOFIX_STALE_BASE_SKIP=true\n"
	assert "source=pr_metadata action=soft_exit" in stdout


def test_fork_dispatch_skips_the_review(tmp_path):
	"""AD-2 keeps github.sha for a dispatched fork head, so the run must not review it (round 5).

	The fork exit only sets CAN_PUSH=false, which no reviewer step checks, so
	without the skip the reviewers read the default branch's files against the
	fork PR's diff.
	"""
	env_text, stdout = _run_metadata_check(
		tmp_path, workspace_sha=EVENT_SHA, metadata_head_sha=HEAD, event_head_sha="", head_repo=FORK_REPOSITORY
	)
	assert env_text == "AUTOFIX_STALE_BASE_SKIP=true\n"
	assert (
		f"AUTOFIX_REVIEW_CROSS_REPO_DISPATCH_SKIP pr=5857 workspace_sha={EVENT_SHA} "
		f"head_repo={FORK_REPOSITORY} target_branch=feature action=soft_exit"
	) in stdout
	assert f"::warning::PR #5857 head is in {FORK_REPOSITORY}, not {REPOSITORY};" in stdout
	assert "AUTOFIX_REVIEW_WORKSPACE_HEAD_MOVED" not in stdout


def test_fork_dispatch_skips_without_payload_or_head_repo(tmp_path):
	"""The skip does not depend on the PR payload, and a deleted fork (null head repo) skips too."""
	(tmp_path / "no_payload").mkdir()
	(tmp_path / "deleted_fork").mkdir()
	env_text, stdout = _run_metadata_check(
		tmp_path / "no_payload", workspace_sha=EVENT_SHA, metadata_head_sha=None, event_head_sha="", head_repo=FORK_REPOSITORY
	)
	assert env_text == "AUTOFIX_STALE_BASE_SKIP=true\n"
	assert "AUTOFIX_REVIEW_CROSS_REPO_DISPATCH_SKIP pr=5857" in stdout
	env_text, stdout = _run_metadata_check(
		tmp_path / "deleted_fork", workspace_sha=EVENT_SHA, metadata_head_sha=HEAD, event_head_sha="", head_repo="null"
	)
	assert env_text == "AUTOFIX_STALE_BASE_SKIP=true\n"
	assert "head_repo=null" in stdout


def test_same_repo_dispatch_with_unmoved_head_keeps_running(tmp_path):
	env_text, stdout = _run_metadata_check(
		tmp_path, workspace_sha=HEAD, metadata_head_sha=HEAD, event_head_sha="", head_repo=REPOSITORY
	)
	assert (env_text, stdout) == ("", "")


def test_cross_repo_dispatch_skip_runs_before_every_exit():
	run = _codex_agent_step("Checkout PR head branch")["run"]
	skip = run.index('echo "AUTOFIX_REVIEW_CROSS_REPO_DISPATCH_SKIP pr=')
	first_exit = re.search(r"^\s*exit \d", run, re.MULTILINE).start()
	assert skip < first_exit


def test_cross_repo_dispatch_log_prefix_is_registered_in_agents_md():
	assert "AUTOFIX_REVIEW_CROSS_REPO_DISPATCH_SKIP pr=" in _workflow_text()
	agents_text = AGENTS_MD.read_text(encoding="utf-8")
	assert "\n- `AUTOFIX_REVIEW_CROSS_REPO_DISPATCH_SKIP`\n" in agents_text
	assert "\nLOG_PREFIX.name=AUTOFIX_REVIEW_CROSS_REPO_DISPATCH_SKIP\n" in agents_text


def test_no_pr_path_and_missing_payload_are_not_compared(tmp_path):
	(tmp_path / "no_pr").mkdir()
	(tmp_path / "no_payload").mkdir()
	env_text, stdout = _run_metadata_check(
		tmp_path / "no_pr", workspace_sha=EVENT_SHA, metadata_head_sha=HEAD, event_head_sha="", head_repo=REPOSITORY, pr_number=""
	)
	assert (env_text, stdout) == ("", "")
	env_text, stdout = _run_metadata_check(
		tmp_path / "no_payload", workspace_sha=HEAD, metadata_head_sha=None, event_head_sha=HEAD, head_repo=FORK_REPOSITORY
	)
	assert (env_text, stdout) == ("", "")


# ---------------------------------------------------------------------------
# End to end on a scratch repository
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"HOME": str(cwd),
		"GIT_AUTHOR_NAME": "t",
		"GIT_AUTHOR_EMAIL": "t@example.com",
		"GIT_COMMITTER_NAME": "t",
		"GIT_COMMITTER_EMAIL": "t@example.com",
		"GIT_CONFIG_NOSYSTEM": "1",
	}
	result = subprocess.run(["git", *args], cwd=cwd, env=env, text=True, capture_output=True, check=True)
	return result.stdout.strip()


def test_dispatched_run_reviewer_workspace_contains_file_added_by_pr_head(tmp_path):
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, "init", "-q", "-b", "main")
	(origin / "README.md").write_text("base\n", encoding="utf-8")
	_git(origin, "add", "README.md")
	_git(origin, "commit", "-q", "-m", "base")
	main_sha = _git(origin, "rev-parse", "HEAD")
	_git(origin, "checkout", "-q", "-b", "claude/feature")
	(origin / "added_by_pr.txt").write_text("new\n", encoding="utf-8")
	_git(origin, "add", "added_by_pr.txt")
	_git(origin, "commit", "-q", "-m", "add file")
	pr_head = _git(origin, "rev-parse", "HEAD")
	_git(origin, "checkout", "-q", "main")

	gate_sha, _ = _run_fragment(
		tmp_path, pr_number="7", head_sha=pr_head, head_repo=REPOSITORY, event_name="workflow_dispatch"
	)
	ref = _resolve_checkout_ref(event_head_sha="", gate_checkout_sha=gate_sha, github_sha=main_sha)

	# actions/checkout: clone and check the resolved ref out into GITHUB_WORKSPACE.
	github_workspace = tmp_path / "workspace"
	_git(tmp_path, "clone", "-q", str(origin), str(github_workspace))
	_git(github_workspace, "checkout", "-q", ref)

	# The reviewer workspace is GITHUB_WORKSPACE (review_run_reviewers.sh).
	assert (github_workspace / "added_by_pr.txt").is_file()
	assert _git(github_workspace, "rev-parse", "HEAD") == pr_head

	# Before the fix the expression fell through to github.sha: no file.
	assert _resolve_checkout_ref(event_head_sha="", gate_checkout_sha="", github_sha=main_sha) == main_sha
