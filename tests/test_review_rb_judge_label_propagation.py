#!/usr/bin/env python3
"""Regression tests for the review-blocked reissue label-propagation fix.

Background
----------
scripts/review_rb_judge.sh handles three judge actions: merge, fix, and
close_and_reissue.  The close_and_reissue branch creates a replacement
issue via `gh issue create`.  Before this fix, the `gh issue create`
call passed no `--label` flag — the reissue inherited only
`ai:clarification` (added later by clarify.yml on issues.opened) and
NOT `ai:orchestrator-managed` from the parent.

The auto-answer fast path in clarify.yml gates strictly on
`has_label("ai:orchestrator-managed") && !forced_reclarify && !is_closed`
(see clarify.yml: "Decide clarify route" step).  Without that label,
an orchestrator-managed parent's review-blocked reissue would stall
in clarification forever — emitting standalone-stall heartbeats while
the orchestrator's parallel judge-addition issue (which IS labelled
`ai:orchestrator-managed` by orchestrate_poll_process.sh) silently
delivered the same work.  See downstream evidence in
shubhodeep1/bitsafe.io issues #41/#43/#44.

The fix
-------
1. The body-fetch loop now captures parent labels in the same REST
   GET (no extra API call per CLAUDE.md §15) into FIRST_ISSUE_LABELS_JSON.
2. The close_and_reissue branch checks FIRST_ISSUE_LABELS_JSON for
   `ai:orchestrator-managed` via `jq -e 'index(...)'`; on hit, it
   appends `--label ai:orchestrator-managed` to the `gh issue create`
   call.  Standalone reissues (parent without the label) get no
   `--label` so their human-driven clarify semantics are preserved.

These tests pin both halves so a future refactor cannot silently
re-orphan the reissue lineage.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RB_JUDGE_SCRIPT = REPO_ROOT / "scripts" / "review_rb_judge.sh"
RB_JUDGE_PROMPT = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


def _rb_judge_text() -> str:
	return RB_JUDGE_SCRIPT.read_text(encoding="utf-8")


def _rb_judge_prompt_text() -> str:
	return RB_JUDGE_PROMPT.read_text(encoding="utf-8")


def _review_autofix_text() -> str:
	return REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")


def _sanitized_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
	env = os.environ.copy()
	for key in (
		"BASH_ENV",
		"ENV",
		"GIT_DIR",
		"GIT_WORK_TREE",
		"GIT_COMMON_DIR",
		"GIT_INDEX_FILE",
		"GIT_OBJECT_DIRECTORY",
		"GIT_ALTERNATE_OBJECT_DIRECTORIES",
	):
		env.pop(key, None)
	if extra:
		env.update(extra)
	return env


def _git(cmd: list[str], *, cwd: Path) -> str:
	result = subprocess.run(
		cmd,
		cwd=str(cwd),
		check=True,
		text=True,
		capture_output=True,
		env=_sanitized_git_env(),
	)
	return result.stdout.strip()


def _bootstrap_repo_with_remote(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str, str]:
	repo_dir = tmp_path / "repo"
	remote_dir = tmp_path / "remote.git"
	repo_dir.mkdir(parents=True, exist_ok=True)
	_git(["git", "init", "--bare", str(remote_dir)], cwd=tmp_path)
	_git(["git", "init"], cwd=repo_dir)
	_git(["git", "checkout", "-b", "main"], cwd=repo_dir)
	_git(["git", "config", "user.name", "tests"], cwd=repo_dir)
	_git(["git", "config", "user.email", "tests@example.com"], cwd=repo_dir)
	for rel, content in files.items():
		path = repo_dir / rel
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content, encoding="utf-8")
	_git(["git", "add", "."], cwd=repo_dir)
	_git(["git", "commit", "-m", "initial"], cwd=repo_dir)
	_git(["git", "remote", "add", "origin", str(remote_dir)], cwd=repo_dir)
	_git(["git", "push", "-u", "origin", "main"], cwd=repo_dir)
	return (
		repo_dir,
		_git(["git", "rev-parse", "HEAD"], cwd=repo_dir),
		_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir),
	)


def _extract_close_and_reissue_branch() -> str:
	"""Pull the body of the `close_and_reissue)` case arm out of the
	real script.  We anchor on the next case label (`*)`) so we don't
	swallow the catch-all branch."""
	text = _rb_judge_text()
	match = re.search(
		r"  close_and_reissue\)\n(?P<branch>.*?)\n    ;;\n\n  \*\)",
		text,
		re.DOTALL,
	)
	if not match:
		raise AssertionError(
			"could not extract close_and_reissue branch from review_rb_judge.sh"
		)
	return match.group("branch")


def _extract_body_fetch_loop() -> str:
	"""Pull the FIRST_ISSUE / FIRST_ISSUE_BODY / FIRST_ISSUE_LABELS_JSON
	body-fetch loop out of the real script.  Anchored on the
	`FIRST_ISSUE=""` declaration through the loop's `done` line so the
	regex follows the loop's lexical block."""
	text = _rb_judge_text()
	match = re.search(
		r'(?P<loop>FIRST_ISSUE=""\n.*?done <<< "\$\{ISSUE_NUMBERS\}")',
		text,
		re.DOTALL,
	)
	if not match:
		raise AssertionError(
			"could not extract body-fetch loop from review_rb_judge.sh"
		)
	return match.group("loop")


def _extract_issue_number_fallback_block() -> str:
	"""Pull the linked-issue fallback block out of the real script.

	Anchored on the top-level `if [ -z "${ISSUE_NUMBERS}" ]` block through
	the cleanup line immediately before `FIRST_ISSUE=""` so tests execute
	the same cached-metadata reuse / fail-open refetch logic the script
	runs in production.
	"""
	lines = _rb_judge_text().splitlines()
	start = None
	for idx, line in enumerate(lines):
		if line == 'if [ -z "${ISSUE_NUMBERS}" ]; then':
			start = idx
			break
	if start is None:
		raise AssertionError(
			"could not locate ISSUE_NUMBERS fallback block in review_rb_judge.sh"
		)

	end = None
	for idx in range(start + 1, len(lines)):
		if lines[idx] == 'FIRST_ISSUE=""':
			end = idx
			break
	if end is None or end <= start:
		raise AssertionError(
			"could not locate the end of the ISSUE_NUMBERS fallback block in review_rb_judge.sh"
		)
	return "\n".join(lines[start:end]).rstrip()


def test_loop_captures_first_issue_labels_json() -> None:
	"""The body-fetch loop must populate FIRST_ISSUE_LABELS_JSON from
	the parent issue's labels.  Static check: a future refactor that
	drops the labels capture would silently disable the propagation."""
	src = _rb_judge_text()

	assert "FIRST_ISSUE_LABELS_JSON=" in src, (
		"review_rb_judge.sh must declare FIRST_ISSUE_LABELS_JSON; the "
		"close_and_reissue branch reads it to decide whether to "
		"propagate ai:orchestrator-managed to the reissue."
	)
	# The loop must populate it from the same `_safe_gh_jq` call that
	# fetches the body — adding a separate `gh api` call would violate
	# CLAUDE.md §15 (GitHub API call hygiene).
	assert "ISSUE_META_JSON=" in src, (
		"review_rb_judge.sh body-fetch loop must store the full issue "
		"JSON so labels and body can be extracted client-side without a "
		"second API call (§15)."
	)
	assert "[(.labels // [])[]?.name]" in src, (
		"review_rb_judge.sh must extract label names from the parent "
		"issue JSON via the canonical jq filter; otherwise the "
		"close_and_reissue branch's `index(\"ai:orchestrator-managed\")` "
		"check would never see a hit."
	)


def test_close_and_reissue_branch_gates_on_orchestrator_managed_label() -> None:
	"""The propagation gate must be exactly
	`index("ai:orchestrator-managed")` against FIRST_ISSUE_LABELS_JSON,
	not a wider/looser check (e.g. any `ai:` label) — that would
	mislabel standalone review-blocked reissues."""
	branch = _extract_close_and_reissue_branch()

	assert "FIRST_ISSUE_LABELS_JSON" in branch, (
		"close_and_reissue branch must reference FIRST_ISSUE_LABELS_JSON; "
		"otherwise it cannot decide whether to propagate the label."
	)
	assert 'jq -e \'index("ai:orchestrator-managed")\'' in branch, (
		"close_and_reissue branch must gate label propagation strictly "
		"on the parent carrying ai:orchestrator-managed — widening the "
		"gate would mislabel standalone (non-orchestrator) reissues."
	)
	# Pin the exact bash array assignment that appends the label flag
	# pair to `gh issue create`.  Matching the literal source line is
	# clearer than a quote-escaped substring search and catches both a
	# missing `--label` arg and a wrong label name in one assertion.
	assert 'RB_PROPAGATE_LABELS+=("--label" "ai:orchestrator-managed")' in branch, (
		"close_and_reissue branch must append `--label ai:orchestrator-managed` "
		"to the gh issue create args via the RB_PROPAGATE_LABELS array."
	)


def _install_mock_gh(bin_dir: Path, state_file: Path) -> None:
	gh_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
if state_path.exists():
	state = json.loads(state_path.read_text(encoding="utf-8"))
else:
	state = {}
args = sys.argv[1:]


def save() -> None:
	state_path.write_text(json.dumps(state), encoding="utf-8")


def first_value(flag: str) -> str:
	for i, arg in enumerate(args):
		if arg == flag and i + 1 < len(args):
			return args[i + 1]
	return ""


state.setdefault("calls", []).append(args)

if args[:2] == ["issue", "create"]:
	# `issue_create_should_fail` flag lets a test exercise the
	# error-handling path in the merge_with_followup branch (when
	# gh issue create fails after merge confirmation, the script
	# must NOT emit judge_handled=true and must NOT swap the
	# linked issue to ai:ready-to-merge).
	state.setdefault("issue_create_args", []).append(args)
	if state.get("issue_create_should_fail", False):
		save()
		sys.stderr.write("mock gh: simulated issue create failure\n")
		sys.exit(1)
	repo = first_value("--repo") or "owner/repo"
	next_num = int(state.get("next_issue_number", 4301))
	state["next_issue_number"] = next_num + 1
	save()
	print(f"https://github.com/{repo}/issues/{next_num}")
	sys.exit(0)

if args[:2] == ["pr", "close"]:
	state.setdefault("pr_close_args", []).append(args)
	save()
	sys.exit(0)

if args[:2] == ["pr", "view"]:
	state.setdefault("pr_view_args", []).append(args)
	head_ref_oid = state.get("pr_view_head_ref_oid", "")
	save()
	if "--jq" in args:
		jq_expr = args[args.index("--jq") + 1]
		if jq_expr == ".headRefOid":
			print(head_ref_oid)
			sys.exit(0)
	print(json.dumps({"headRefOid": head_ref_oid}))
	sys.exit(0)

if args[:2] == ["label", "create"]:
	state.setdefault("label_create_args", []).append(args)
	save()
	sys.exit(0)

if args[:1] == ["api"]:
	# Find the path arg (first non-flag, non-flag-value).  We treat the
	# first positional after `api` as the path.  Caller-configured
	# `api_responses` is keyed by substring match against that path.
	path = ""
	for arg in args[1:]:
		if not arg.startswith("-"):
			path = arg
			break
	state.setdefault("api_calls", []).append(args)
	api_responses = state.get("api_responses", {}) or {}
	matched = None
	for pattern, resp in api_responses.items():
		if pattern and pattern in path:
			matched = resp
			break
	save()
	if matched is not None:
		# Honour `--jq` server-side filtering: emit the filtered string,
		# not the raw JSON, so callers that pre-fix used `--jq '.body'`
		# still see the same shape (this matters for tests that exercise
		# behaviour beyond the loop refactor).
		jq_filter = ""
		for i, arg in enumerate(args):
			if arg == "--jq" and i + 1 < len(args):
				jq_filter = args[i + 1]
				break
		if jq_filter:
			# Limited support: handle `.body // ""` and `.state` to keep
			# the mock predictable; anything else falls through to raw
			# JSON so a future test can extend behaviour as needed.
			if jq_filter == ".body // \"\"" or jq_filter == ".body":
				print(matched.get("body", ""))
			elif jq_filter == '.title + " " + (.body // "")':
				print(f"{matched.get('title', '') or ''} {matched.get('body', '') or ''}")
			elif jq_filter == ".state":
				print(matched.get("state", ""))
			else:
				print(json.dumps(matched))
		else:
			print(json.dumps(matched))
	sys.exit(0)

save()
sys.exit(0)
'''
	mock_path = bin_dir / "gh"
	mock_path.write_text(gh_script, encoding="utf-8")
	mock_path.chmod(0o755)
	state_file.write_text("{}", encoding="utf-8")


def _build_harness(
	branch: str,
	runtime_dir: Path,
	github_output: Path,
	*,
	enable_xpg_echo: bool = False,
) -> str:
	"""Wrap the extracted close_and_reissue branch in a self-contained
	bash harness with stubs for the helpers it depends on."""
	xpg_echo_line = "shopt -s xpg_echo\n\n" if enable_xpg_echo else ""
	return f"""#!/usr/bin/env bash
set -euo pipefail

{xpg_echo_line}gh_retry() {{ "$@"; }}
ensure_label_exists() {{ printf '%s\\n' "$1" >> "${{ENSURE_LABELS_FILE}}"; }}
_resilient_phase_swap() {{ :; }}
_safe_gh_jq() {{ :; }}

GITHUB_OUTPUT="{github_output}"

case "${{RB_ACTION}}" in
  close_and_reissue)
{branch}
    ;;
esac
"""


def _run_close_and_reissue(
	parent_label_set: list[str],
	*,
	judge_payload: dict | None = None,
	reissue_preserve_baseline_enabled: str = "false",
	repo_files: dict[str, str] | None = None,
	pr_view_head_ref_oid: str | None = None,
	precreate_baseline_branch: bool = False,
	enable_xpg_echo: bool = False,
) -> dict:
	"""Run the close_and_reissue branch with FIRST_ISSUE_LABELS_JSON
	pre-seeded to ``parent_label_set`` and return the captured gh
	mock state."""
	branch = _extract_close_and_reissue_branch()

	with tempfile.TemporaryDirectory(prefix="test_review_rb_judge_") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		bin_dir = tmp_path / "bin"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		bin_dir.mkdir(parents=True, exist_ok=True)

		gh_state_file = runtime_dir / "gh_state.json"
		_install_mock_gh(bin_dir, gh_state_file)
		mock_state: dict[str, object] = {}
		run_cwd = tmp_path
		repo_head_before = ""
		repo_branch_before = ""
		if repo_files is not None:
			run_cwd, repo_head_before, repo_branch_before = _bootstrap_repo_with_remote(tmp_path, repo_files)
			resolved_head_ref_oid = pr_view_head_ref_oid or repo_head_before
			if resolved_head_ref_oid == "__UPPER_REPO_HEAD__":
				resolved_head_ref_oid = repo_head_before.upper()
			mock_state["pr_view_head_ref_oid"] = resolved_head_ref_oid
			if precreate_baseline_branch:
				expected_baseline_branch = f"ai/reissue-baseline/pr-42-{repo_head_before[:12]}-777-1"
				_git(["git", "branch", expected_baseline_branch], cwd=run_cwd)
		elif pr_view_head_ref_oid is not None:
			mock_state["pr_view_head_ref_oid"] = pr_view_head_ref_oid
		gh_state_file.write_text(json.dumps(mock_state), encoding="utf-8")

		labels_file = runtime_dir / "ensure_labels.txt"
		labels_file.write_text("", encoding="utf-8")
		github_output = runtime_dir / "github_output.txt"
		github_output.write_text("", encoding="utf-8")

		script_path = runtime_dir / "rb_branch_harness.sh"
		script_path.write_text(
			_build_harness(
				branch,
				runtime_dir,
				github_output,
				enable_xpg_echo=enable_xpg_echo,
			),
			encoding="utf-8",
		)
		script_path.chmod(0o755)

		if judge_payload is None:
			judge_payload = {
				"action": "close_and_reissue",
				"justification": "rework needed",
				"new_issue": {
					"title": "Reissue: redo approach",
					"body": "Try again with smaller scope.",
				},
			}
		judge_json = json.dumps(judge_payload)

		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"ENSURE_LABELS_FILE": str(labels_file),
			"REPOSITORY": "owner/repo",
			"PR_NUMBER": "42",
			"ISSUE_NUMBERS": "41",
			"FIRST_ISSUE": "41",
			"FIRST_ISSUE_LABELS_JSON": json.dumps(parent_label_set),
			"JUDGE_JSON": judge_json,
			"RB_ACTION": "close_and_reissue",
			"REISSUE_PRESERVE_BASELINE_ENABLED": reissue_preserve_baseline_enabled,
			"GITHUB_RUN_ID": "777",
			"GITHUB_RUN_ATTEMPT": "1",
		}
		run_env = _sanitized_git_env(env)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(run_cwd),
			env=run_env,
			text=True,
			capture_output=True,
			timeout=60,
		)
		assert proc.returncode == 0, (
			f"harness exited {proc.returncode}\n"
			f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		)

		state = json.loads(gh_state_file.read_text(encoding="utf-8"))
		state["_ensure_labels"] = labels_file.read_text(encoding="utf-8").strip().splitlines()
		state["_stdout"] = proc.stdout
		state["_stderr"] = proc.stderr
		state["_github_output"] = github_output.read_text(encoding="utf-8")
		if repo_files is not None:
			state["_repo_head_before"] = repo_head_before
			state["_repo_head_after"] = _git(["git", "rev-parse", "HEAD"], cwd=run_cwd)
			state["_repo_branch_before"] = repo_branch_before
			state["_repo_branch_after"] = _git(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=run_cwd)
			state["_repo_status_after"] = _git(["git", "status", "--short"], cwd=run_cwd)
			state["_remote_heads"] = _git(["git", "ls-remote", "--heads", "origin"], cwd=run_cwd)
		return state


def test_orchestrator_managed_parent_propagates_label_to_reissue() -> None:
	"""Parent carries ai:orchestrator-managed → reissue must be
	created with `--label ai:orchestrator-managed`.  Without this, the
	auto-answer fast path in clarify.yml never fires and the reissue
	stalls in clarification."""
	state = _run_close_and_reissue(["ai:orchestrator-managed", "ai:closed"])

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1, (
		f"expected exactly one `gh issue create` call, got {len(creates)}: {creates}"
	)
	args = creates[0]
	assert "--label" in args, (
		f"reissue must carry --label when parent is orchestrator-managed; "
		f"got args: {args}"
	)
	# Find the value that follows --label
	idx = args.index("--label")
	assert args[idx + 1] == "ai:orchestrator-managed", (
		f"reissue --label must be exactly 'ai:orchestrator-managed'; "
		f"got: {args[idx + 1]!r}"
	)

	# ensure_label_exists must be called for the propagated label so the
	# repo has the label definition before `gh issue create` references it.
	assert "ai:orchestrator-managed" in state["_ensure_labels"], (
		"ensure_label_exists must be invoked for ai:orchestrator-managed "
		"before the gh issue create call; otherwise a fresh consumer repo "
		"could 422 on an unknown label."
	)


def test_standalone_parent_does_not_propagate_orchestrator_managed_label() -> None:
	"""Parent does NOT carry ai:orchestrator-managed → reissue must be
	created WITHOUT that label.  Standalone clarifications must keep
	their human-driven semantics — widening the propagation would
	mislabel non-orchestrator reissues as orchestrator-managed and
	cause clarify.yml to auto-answer human PRs."""
	state = _run_close_and_reissue(["bug", "good first issue"])

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1, (
		f"expected exactly one `gh issue create` call, got {len(creates)}: {creates}"
	)
	args = creates[0]
	assert "ai:orchestrator-managed" not in args, (
		f"standalone reissue must NOT carry ai:orchestrator-managed; "
		f"got args: {args}"
	)


def test_empty_parent_label_set_does_not_propagate() -> None:
	"""No linked-issue labels (e.g. parent fetch failed → empty array)
	→ reissue must be created without `--label ai:orchestrator-managed`.
	Conservative fail-closed default; an extra label on a reissue is
	harder to undo than its absence."""
	state = _run_close_and_reissue([])

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1
	args = creates[0]
	assert "ai:orchestrator-managed" not in args, (
		f"reissue must NOT inherit ai:orchestrator-managed when parent "
		f"label set is empty; got args: {args}"
	)


def test_review_blocked_prompt_includes_phase_e_schema_fields() -> None:
	prompt = _rb_judge_prompt_text()

	assert '"reissue_mode": "spot-fix" | "redo"' in prompt, (
		"mode-judge-review-blocked.txt must require the new reissue_mode field "
		"so the judge can choose spot-fix vs redo."
	)
	assert '"remaining_issues": [' in prompt, (
		"mode-judge-review-blocked.txt must include the structured remaining_issues[] sibling."
	)
	assert '"file": "<repo-relative path>"' in prompt
	assert '"line_start": <integer>' in prompt
	assert '"line_end": <integer>' in prompt
	assert '"symptom": "<brief grounded gap>"' in prompt


def test_review_autofix_wires_reissue_preserve_baseline_flag_default_false() -> None:
	wf = _review_autofix_text()
	assert "REISSUE_PRESERVE_BASELINE_ENABLED: ${{ vars.REISSUE_PRESERVE_BASELINE_ENABLED || 'false' }}" in wf, (
		"review_autofix.yml must pass REISSUE_PRESERVE_BASELINE_ENABLED to the "
		"review-blocked judge and default it to false for Phase E bake-out safety."
	)


def test_close_and_reissue_spot_fix_preserves_baseline_branch_and_keeps_caller_checkout_clean() -> None:
	state = _run_close_and_reissue(
		["ai:orchestrator-managed"],
		judge_payload={
			"action": "close_and_reissue",
			"reissue_mode": "spot-fix",
			"justification": "Approach is correct; only a small follow-up is needed.",
			"remaining_issues": [
				{"file": "src/app.py", "line_start": 10, "line_end": 14, "symptom": "Guard missing"},
				{"file": "src/app.py", "line_start": 15, "line_end": 16, "symptom": "Duplicate anchor should dedupe"},
				{"file": "README.md", "line_start": 1, "line_end": 2, "symptom": "Docs must mention the edge case"},
			],
			"new_issue": {
				"title": "Reissue: surgical follow-up",
				"body": "Keep the prior implementation and patch only the grounded gaps.",
			},
		},
		reissue_preserve_baseline_enabled="true",
		repo_files={
			"src/app.py": "print('hello')\n",
			"README.md": "hello\n",
		},
	)

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1, f"expected one reissue create call, got: {creates}"
	body = creates[0][creates[0].index("--body") + 1]
	match = re.search(r"prior_pr_baseline_branch: ([^\n]+)", body)
	assert match, f"expected prior_pr_baseline_branch metadata in body:\n{body}"
	baseline_branch = match.group(1).strip()
	assert baseline_branch.startswith("ai/reissue-baseline/pr-42-"), baseline_branch
	expected_footer = textwrap.dedent(
		f"""\
		---
		**Review-blocked reissue metadata**
		- Replaces: #41 (PR #42 closed — approach rework)
		- Type: review-blocked-reissue
		- prior_pr_baseline_branch: {baseline_branch}
		- files_touched:
		  - src/app.py
		  - README.md"""
	)
	assert body.rstrip().endswith(expected_footer.rstrip()), (
		"spot-fix reissue body must emit the canonical footer-scoped baseline metadata contract"
	)
	assert body.count("  - src/app.py") == 1, (
		"spot-fix reissue body must dedupe repeated remaining_issues[].file entries"
	)
	assert "  - README.md" in body
	assert f"refs/heads/{baseline_branch}" in state.get("_remote_heads", ""), (
		"spot-fix must push the preserved baseline branch to origin before creating the reissue"
	)
	assert "REISSUE_BASELINE_PRESERVED" in state["_stdout"], (
		"spot-fix success path must emit the REISSUE_BASELINE_PRESERVED log prefix"
	)
	assert "REISSUE_MODE requested_raw=spot-fix effective=spot-fix" in state["_stdout"]
	assert "judge_handled=true" in state["_github_output"]
	assert state.get("_repo_head_after") == state.get("_repo_head_before"), (
		"git worktree preservation path must not change the caller checkout's HEAD commit"
	)
	assert state.get("_repo_branch_after") == state.get("_repo_branch_before") == "main", (
		"git worktree preservation path must not switch the caller checkout off its original branch"
	)
	assert state.get("_repo_status_after", "") == "", (
		"git worktree preservation path must leave the caller checkout clean"
	)


def test_close_and_reissue_spot_fix_lowercases_baseline_branch_sha_prefix() -> None:
	state = _run_close_and_reissue(
		["ai:orchestrator-managed"],
		judge_payload={
			"action": "close_and_reissue",
			"reissue_mode": "spot-fix",
			"justification": "Uppercase head SHAs must still produce a trusted preserved baseline branch.",
			"remaining_issues": [
				{"file": "src/app.py", "line_start": 10, "line_end": 14, "symptom": "Guard missing"},
			],
			"new_issue": {
				"title": "Reissue: lowercase preserved baseline",
				"body": "Keep the prior implementation and patch only the grounded gap.",
			},
		},
		reissue_preserve_baseline_enabled="true",
		repo_files={"src/app.py": "print('hello')\n"},
		pr_view_head_ref_oid="__UPPER_REPO_HEAD__",
	)

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1, f"expected one reissue create call, got: {creates}"
	body = creates[0][creates[0].index("--body") + 1]
	match = re.search(r"prior_pr_baseline_branch: ([^\n]+)", body)
	assert match, f"expected prior_pr_baseline_branch metadata in body:\n{body}"
	baseline_branch = match.group(1).strip()
	expected_head_sha = str(state["_repo_head_before"]).lower()
	assert baseline_branch == f"ai/reissue-baseline/pr-42-{expected_head_sha[:12]}-777-1"
	assert f"refs/heads/{baseline_branch}" in state.get("_remote_heads", "")
	assert f"REISSUE_BASELINE_PRESERVED branch={baseline_branch} head_sha={expected_head_sha}" in state["_stdout"]


def test_close_and_reissue_invalid_reissue_mode_falls_back_to_redo() -> None:
	state = _run_close_and_reissue(
		["ai:orchestrator-managed"],
		judge_payload={
			"action": "close_and_reissue",
			"reissue_mode": "unknown-mode",
			"justification": "Malformed output from the judge should fail open.",
			"remaining_issues": [
				{"file": "src/app.py", "line_start": 1, "line_end": 2, "symptom": "Unused in redo fallback"},
			],
			"new_issue": {
				"title": "Reissue: redo",
				"body": "Re-derive the patch from scratch.",
			},
		},
		reissue_preserve_baseline_enabled="true",
	)

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1
	body = creates[0][creates[0].index("--body") + 1]
	assert "prior_pr_baseline_branch:" not in body
	assert "files_touched:" not in body
	assert "REISSUE_MODE requested_raw=unknown-mode effective=redo" in state["_stdout"], (
		"invalid reissue_mode must fail open back to redo instead of attempting spot-fix"
	)
	assert "REISSUE_BASELINE_PRESERVED" not in state["_stdout"]


def test_close_and_reissue_preserves_judge_json_bytes_under_xpg_echo() -> None:
	state = _run_close_and_reissue(
		["ai:orchestrator-managed"],
		judge_payload={
			"action": "close_and_reissue",
			"reissue_mode": "redo",
			"justification": "Quoted reissues should not be dropped.",
			"new_issue": {
				"title": 'Reissue: handle "quoted" follow-up',
				"body": 'First line keeps "quotes" and literal \\n intact.\nSecond line stays attached.',
			},
		},
		enable_xpg_echo=True,
	)

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1, (
		"close_and_reissue must still create the replacement issue when bash xpg_echo is enabled"
	)
	args = creates[0]
	title = args[args.index("--title") + 1]
	body = args[args.index("--body") + 1]
	assert title == 'Reissue: handle "quoted" follow-up'
	assert body.startswith('First line keeps "quotes" and literal \\n intact.\nSecond line stays attached.')


def test_close_and_reissue_spot_fix_invalid_remaining_issue_file_falls_back_to_redo() -> None:
	state = _run_close_and_reissue(
		["ai:orchestrator-managed"],
		judge_payload={
			"action": "close_and_reissue",
			"reissue_mode": "spot-fix",
			"justification": "Path validation must reject unsafe files_touched metadata.",
			"remaining_issues": [
				{"file": "../escape.sh", "line_start": 1, "line_end": 1, "symptom": "Unsafe path"},
			],
			"new_issue": {
				"title": "Reissue: invalid path should redo",
				"body": "Fallback to redo when the structured file list is unsafe.",
			},
		},
		reissue_preserve_baseline_enabled="true",
		repo_files={"src/app.py": "print('hello')\n"},
	)

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1
	body = creates[0][creates[0].index("--body") + 1]
	assert "prior_pr_baseline_branch:" not in body
	assert "files_touched:" not in body
	assert "REISSUE_BASELINE_DISCARDED requested=spot-fix reason=invalid_file_path:../escape.sh" in state["_stdout"], (
		"unsafe remaining_issues[].file entries must discard the spot-fix baseline and redo instead"
	)
	assert "REISSUE_MODE requested_raw=spot-fix effective=redo" in state["_stdout"]


def test_close_and_reissue_spot_fix_setup_failure_uses_setup_reason_and_surfaces_git_error() -> None:
	state = _run_close_and_reissue(
		["ai:orchestrator-managed"],
		judge_payload={
			"action": "close_and_reissue",
			"reissue_mode": "spot-fix",
			"justification": "Baseline creation should fail open when local setup fails.",
			"remaining_issues": [
				{"file": "src/app.py", "line_start": 1, "line_end": 2, "symptom": "Needs a focused fix"},
			],
			"new_issue": {
				"title": "Reissue: setup failure redo",
				"body": "Fallback to redo when the baseline branch cannot be prepared locally.",
			},
		},
		reissue_preserve_baseline_enabled="true",
		repo_files={"src/app.py": "print('hello')\n"},
		precreate_baseline_branch=True,
	)

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1
	body = creates[0][creates[0].index("--body") + 1]
	assert "prior_pr_baseline_branch:" not in body
	assert "files_touched:" not in body
	assert "REISSUE_BASELINE_DISCARDED requested=spot-fix reason=baseline_branch_setup_failed" in state["_stdout"], (
		"pre-push baseline setup failures must log the setup-specific discard reason"
	)
	assert "baseline_branch_push_failed" not in state["_stdout"]
	assert "fatal: a branch named" in state["_stderr"], (
		"git checkout -b setup failures must surface the underlying git error on stderr"
	)


def _run_body_fetch_loop(issue_responses: dict[str, dict], issue_numbers: str) -> dict:
	"""Run the real body-fetch loop block against a mocked `gh api` so
	the actual jq pipeline (`[(.labels // [])[]?.name]` plus the body
	extract) is exercised end-to-end.

	``issue_responses`` maps an API path substring (e.g.
	``"issues/41"``) to the mocked GitHub issue JSON the mock returns.
	``issue_numbers`` is the newline-separated string fed to the loop's
	``ISSUE_NUMBERS`` reader.

	Returns the captured ``FIRST_ISSUE``, ``FIRST_ISSUE_BODY``, and
	``FIRST_ISSUE_LABELS_JSON`` values."""
	loop = _extract_body_fetch_loop()

	with tempfile.TemporaryDirectory(prefix="test_rb_judge_loop_") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		bin_dir = tmp_path / "bin"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		bin_dir.mkdir(parents=True, exist_ok=True)

		gh_state_file = runtime_dir / "gh_state.json"
		_install_mock_gh(bin_dir, gh_state_file)
		gh_state_file.write_text(
			json.dumps({"api_responses": issue_responses}),
			encoding="utf-8",
		)

		capture_file = runtime_dir / "captured.env"
		# _safe_gh_jq is the gh_helpers.sh wrapper.  Substitute a thin
		# bash function that forwards directly to the mocked `gh api`
		# so the loop's actual jq filters run against real shell output.
		harness = f"""#!/usr/bin/env bash
set -euo pipefail

_safe_gh_jq() {{ gh api "$@"; }}

ISSUE_NUMBERS="${{ISSUE_NUMBERS_INPUT}}"

{loop}

{{
  printf 'FIRST_ISSUE=%s\\n' "${{FIRST_ISSUE}}"
  printf 'FIRST_ISSUE_BODY=%s\\n' "${{FIRST_ISSUE_BODY}}"
  printf 'FIRST_ISSUE_LABELS_JSON=%s\\n' "${{FIRST_ISSUE_LABELS_JSON}}"
}} > "${{CAPTURE_FILE}}"
"""
		script_path = runtime_dir / "loop_harness.sh"
		script_path.write_text(harness, encoding="utf-8")
		script_path.chmod(0o755)

		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"REPOSITORY": "owner/repo",
			"ISSUE_NUMBERS_INPUT": issue_numbers,
			"CAPTURE_FILE": str(capture_file),
		}
		run_env = _sanitized_git_env(env)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=run_env,
			text=True,
			capture_output=True,
			timeout=60,
		)
		assert proc.returncode == 0, (
			f"loop harness exited {proc.returncode}\n"
			f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		)

		captured = {}
		for raw in capture_file.read_text(encoding="utf-8").splitlines():
			if "=" in raw:
				k, _, v = raw.partition("=")
				captured[k] = v
		return captured


def _run_issue_number_fallback(*, pr_meta: str, fallback_pr_response: dict | None = None) -> dict:
	"""Run the real linked-issue fallback block against mocked `gh`.

	``pr_meta`` seeds the early `_pr_meta` cache captured before the
	linked-issue lookup.  ``fallback_pr_response`` is the mocked
	`pulls/{pr}` response returned only when the block falls back to a
	secondary `_safe_gh_jq` fetch.
	"""
	block = _extract_issue_number_fallback_block()

	with tempfile.TemporaryDirectory(prefix="test_rb_judge_issue_fallback_") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		bin_dir = tmp_path / "bin"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		bin_dir.mkdir(parents=True, exist_ok=True)

		gh_state_file = runtime_dir / "gh_state.json"
		_install_mock_gh(bin_dir, gh_state_file)
		mock_state: dict[str, object] = {}
		if fallback_pr_response is not None:
			mock_state["api_responses"] = {"pulls/42": fallback_pr_response}
		gh_state_file.write_text(json.dumps(mock_state), encoding="utf-8")

		capture_file = runtime_dir / "captured.env"
		helper_path = (REPO_ROOT / "scripts" / "gh_helpers.sh").as_posix()
		harness = f"""#!/usr/bin/env bash
set -euo pipefail

source "{helper_path}"
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{ gh api "$@"; }}

REPOSITORY="owner/repo"
PR_NUMBER="42"
ISSUE_NUMBERS=""
_pr_meta="${{PR_META_INPUT}}"

{block}

{{
  printf 'ISSUE_NUMBERS=%s\\n' "${{ISSUE_NUMBERS}}"
}} > "${{CAPTURE_FILE}}"
"""
		script_path = runtime_dir / "issue_fallback_harness.sh"
		script_path.write_text(harness, encoding="utf-8")
		script_path.chmod(0o755)

		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"PR_META_INPUT": pr_meta,
			"CAPTURE_FILE": str(capture_file),
		}
		run_env = _sanitized_git_env(env)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=run_env,
			text=True,
			capture_output=True,
			timeout=60,
		)
		assert proc.returncode == 0, (
			f"issue fallback harness exited {proc.returncode}\n"
			f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		)

		captured = {}
		for raw in capture_file.read_text(encoding="utf-8").splitlines():
			if "=" in raw:
				k, _, v = raw.partition("=")
				captured[k] = v

		state = json.loads(gh_state_file.read_text(encoding="utf-8"))
		state["_captured"] = captured
		state["_stdout"] = proc.stdout
		state["_stderr"] = proc.stderr
		return state


def _matching_api_calls(state: dict, path_fragment: str) -> list[list[str]]:
	return [
		call for call in state.get("api_calls", [])
		if any(path_fragment in arg for arg in call)
	]


def test_loop_jq_filter_extracts_orchestrator_managed_from_realistic_payload() -> None:
	"""End-to-end runtime test: feed the real body-fetch loop a
	GitHub-shape issue JSON via mocked `gh api` and assert the actual
	jq filter populates FIRST_ISSUE_LABELS_JSON with the parent's
	label names.

	Without this, the other runtime tests (which pre-seed
	FIRST_ISSUE_LABELS_JSON via env and stub _safe_gh_jq to a no-op)
	would not catch a future jq-filter typo or a GitHub-shape change
	in the `.labels` field — the propagation gate would silently see
	`[]` and skip."""
	captured = _run_body_fetch_loop(
		issue_responses={
			"issues/41": {
				"number": 41,
				"title": "Orchestrator-managed parent",
				"body": "Build feature X.",
				"state": "closed",
				"labels": [
					{"id": 1, "name": "ai:orchestrator-managed", "color": "bfdadc"},
					{"id": 2, "name": "ai:closed", "color": "6a737d"},
				],
			},
		},
		issue_numbers="41",
	)
	assert captured.get("FIRST_ISSUE") == "41"
	assert captured.get("FIRST_ISSUE_BODY") == "Build feature X."
	labels = json.loads(captured.get("FIRST_ISSUE_LABELS_JSON", "[]"))
	assert labels == ["ai:orchestrator-managed", "ai:closed"], (
		f"expected the loop's jq filter to extract both label names from a "
		f"realistic GitHub /issues/N response; got {labels}"
	)


def test_loop_jq_filter_handles_null_labels_field() -> None:
	"""GitHub shape variation: some payloads return ``"labels": null``
	(rare but documented).  The `// []` defensive default in the jq
	filter must collapse that to an empty list, not error out — a
	future filter rewrite that dropped the safe default would silently
	break propagation here."""
	captured = _run_body_fetch_loop(
		issue_responses={
			"issues/41": {
				"number": 41,
				"body": "Body present",
				"labels": None,
			},
		},
		issue_numbers="41",
	)
	assert captured.get("FIRST_ISSUE") == "41"
	assert json.loads(captured.get("FIRST_ISSUE_LABELS_JSON", "null")) == []


def test_loop_jq_filter_handles_missing_labels_key() -> None:
	"""If the labels key is absent entirely (defensive: malformed or
	stripped response), the filter must yield an empty list — not
	error and not propagate stale state from a previous iteration."""
	captured = _run_body_fetch_loop(
		issue_responses={
			"issues/41": {
				"number": 41,
				"body": "Body present",
			},
		},
		issue_numbers="41",
	)
	assert json.loads(captured.get("FIRST_ISSUE_LABELS_JSON", "null")) == []


def test_loop_pins_labels_to_first_issue_even_when_body_comes_from_later_issue() -> None:
	"""When the first linked issue has an empty body, the loop falls
	back to a later issue's body — pre-existing behaviour.  The
	label-propagation fix MUST keep FIRST_ISSUE_LABELS_JSON pinned to
	the first issue's labels (which match the reissue footer's
	``Replaces #FIRST_ISSUE``), not jump to the later issue's labels.

	This nails down the contract grok-4.1-fast flagged on PR #2267:
	labels follow FIRST_ISSUE, not whichever issue contributed the body."""
	captured = _run_body_fetch_loop(
		issue_responses={
			"issues/41": {
				"number": 41,
				"body": "",
				"labels": [{"name": "ai:orchestrator-managed"}],
			},
			"issues/42": {
				"number": 42,
				"body": "Fallback body",
				"labels": [{"name": "bug"}],
			},
		},
		issue_numbers="41\n42",
	)
	assert captured.get("FIRST_ISSUE") == "41", (
		"FIRST_ISSUE must be the first linked issue, not the body-source"
	)
	assert captured.get("FIRST_ISSUE_BODY") == "Fallback body", (
		"pre-existing behaviour: empty first body → fall back to next issue's body"
	)
	labels = json.loads(captured.get("FIRST_ISSUE_LABELS_JSON", "[]"))
	assert labels == ["ai:orchestrator-managed"], (
		f"FIRST_ISSUE_LABELS_JSON must follow FIRST_ISSUE (#41), not the "
		f"body-source (#42); got {labels}"
	)


def test_issue_number_fallback_reuses_cached_pr_metadata_without_refetch() -> None:
	state = _run_issue_number_fallback(
		pr_meta=json.dumps({
			"title": "Fixes #41",
			"body": "Strict fallback should reuse cached PR metadata.",
		}),
	)

	assert state["_captured"].get("ISSUE_NUMBERS") == "41", (
		"linked-issue fallback must still extract issue numbers from cached PR title/body"
	)
	pull_calls = _matching_api_calls(state, "pulls/42")
	assert pull_calls == [], (
		"when cached _pr_meta already contains reusable title/body text, the fallback "
		"must not re-fetch pulls/42"
	)


def test_issue_number_fallback_refetches_when_cached_metadata_is_unusable() -> None:
	state = _run_issue_number_fallback(
		pr_meta="{}",
		fallback_pr_response={
			"title": "owner/repo/issues/52",
			"body": "Fallback GET should preserve strict repo-scoped path extraction.",
		},
	)

	assert state["_captured"].get("ISSUE_NUMBERS") == "52", (
		"when cached _pr_meta is unusable, the fallback must still extract issue numbers "
		"from the secondary pulls/{pr} fetch"
	)
	pull_calls = _matching_api_calls(state, "pulls/42")
	assert len(pull_calls) == 1, (
		"unusable cached _pr_meta must fall open to exactly one secondary pulls/{pr} fetch"
	)
	assert "--jq" in pull_calls[0] and '.title + " " + (.body // "")' in pull_calls[0], (
		"the secondary pulls/{pr} fetch must preserve the existing title/body jq projection"
	)


def test_issue_number_fallback_ignores_bare_issue_mentions_in_cached_pr_metadata() -> None:
	state = _run_issue_number_fallback(
		pr_meta=json.dumps({
			"title": "Linked issue #41",
			"body": "Bare prose mentions must stay non-actionable.",
		}),
	)

	assert state["_captured"].get("ISSUE_NUMBERS", "") == "", (
		"bare 'issue #N' mentions must not resolve linked issues in the judge fallback"
	)
	assert _matching_api_calls(state, "pulls/42") == [], (
		"non-empty cached PR metadata must not trigger a fallback pulls/{pr} refetch just because strict parsing found no issue links"
	)


def test_issue_number_fallback_ignores_bare_issue_path_mentions_in_cached_pr_metadata() -> None:
	state = _run_issue_number_fallback(
		pr_meta=json.dumps({
			"title": "Docs note about issues/41",
			"body": "Bare issues/N paths must stay non-actionable.",
		}),
	)

	assert state["_captured"].get("ISSUE_NUMBERS", "") == "", (
		"bare 'issues/N' mentions must not resolve linked issues in the judge fallback"
	)
	assert _matching_api_calls(state, "pulls/42") == [], (
		"non-empty cached PR metadata must not refetch pulls/{pr} for ignored bare issues/N mentions"
	)


# =============================================================================
# merge_with_followup branch coverage
# =============================================================================
#
# Pins the same label-propagation contract for the merge_with_followup
# action (added in PR #2519) AND the new safety gates introduced when
# review comments flagged orphan-risk on the original implementation:
#   - Refuse the action when followup_issue title/body are missing.
#   - Only label / create follow-up / emit judge_handled when the merge
#     can be confirmed (mergeable=true + auto-merge enrolled, or PR
#     already merged).
# Without these tests, a future refactor that re-orphans the follow-up
# tracking issue (e.g. by moving the issue creation outside the
# MERGE_CONFIRMED gate) would slip through.


def _extract_merge_with_followup_branch() -> str:
	"""Pull the body of the `merge_with_followup)` case arm out of the
	real script.  Anchored on the next case label (`close_and_reissue)`)
	so we don't swallow the rest of the case statement."""
	text = _rb_judge_text()
	match = re.search(
		r"  merge_with_followup\)\n(?P<branch>.*?)\n    ;;\n\n  close_and_reissue\)",
		text,
		re.DOTALL,
	)
	if not match:
		raise AssertionError(
			"could not extract merge_with_followup branch from review_rb_judge.sh"
		)
	return match.group("branch")


def _build_merge_with_followup_harness(
	branch: str,
	github_output: Path,
	*,
	enable_xpg_echo: bool = False,
) -> str:
	"""Wrap the extracted merge_with_followup branch in a self-contained
	bash harness with stubs for the helpers it depends on.

	Differences from `_build_harness` (close_and_reissue):
	  - `_safe_gh_jq` forwards to `gh api` so the mergeability-poll loop
	    sees real (mocked) PR JSON rather than no-op'ing into empty
	    output.  Without this the MERGE_CONFIRMED gate can never flip
	    true and every test would skip the issue-create path.
	  - `sleep` is no-op'd so the mergeability-poll backoff (which the
	    real script measures in seconds) doesn't slow tests.
	"""
	xpg_echo_line = "shopt -s xpg_echo\n\n" if enable_xpg_echo else ""
	return f"""#!/usr/bin/env bash
set -euo pipefail

{xpg_echo_line}gh_retry() {{ "$@"; }}
ensure_label_exists() {{ printf '%s\\n' "$1" >> "${{ENSURE_LABELS_FILE}}"; }}
_resilient_phase_swap() {{ :; }}
_safe_gh_jq() {{ gh api "$@"; }}
sleep() {{ :; }}

GITHUB_OUTPUT="{github_output}"

case "${{RB_ACTION}}" in
  merge_with_followup)
{branch}
    ;;
esac
"""


def _run_merge_with_followup(
	parent_label_set: list[str],
	followup_title: str = "Follow-up: wire deriveX into production",
	followup_body: str = "Acceptance criteria: a production caller invokes deriveX with the same args the unit tests use.",
	pr_state: str = "open",
	pr_mergeable: object = True,  # bool or None (=mergeability still computing)
	pr_merged: bool = False,  # GitHub's `.merged` field — authoritative did-this-land signal
	pr_head_sha: str = "abcdef1234567890abcdef1234567890abcdef12",
	enable_auto_merge: str = "true",
	issue_create_should_fail: bool = False,
	check_runs_state: str = "success",  # "success" (all complete + green) or "pending" (one in_progress)
	enable_xpg_echo: bool = False,
) -> dict:
	"""Run the merge_with_followup branch with a mocked PR mergeability
	state and judge JSON.  Returns the captured gh-mock state plus the
	contents of GITHUB_OUTPUT so callers can assert on judge_handled /
	judge_action emission.

	``pr_merged`` mirrors GitHub's REST API shape — a merged PR is
	reported as ``state=closed`` + ``merged=true`` (NEVER
	``state=merged``).  The script's MERGE_CONFIRMED ladder reads
	``.merged`` to detect the already-merged case; tests must use this
	parameter (not ``pr_state``) to pin that branch.

	``issue_create_should_fail`` causes the mock gh to return non-zero
	from ``gh issue create`` so the test can exercise the error-handling
	path (script must skip label swap + judge_handled emission)."""
	branch = _extract_merge_with_followup_branch()

	with tempfile.TemporaryDirectory(prefix="test_rb_judge_mwf_") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		bin_dir = tmp_path / "bin"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		bin_dir.mkdir(parents=True, exist_ok=True)

		gh_state_file = runtime_dir / "gh_state.json"
		_install_mock_gh(bin_dir, gh_state_file)

		# Seed the mock's api_responses with a PR-mergeability shape
		# keyed by the URL path suffix the script will call.  Substring
		# match is what the mock uses, so "pulls/42" is enough.
		# Include `head.sha` so the check-runs gate has a SHA to query
		# against (matches GitHub's real shape).
		pr_response: dict = {
			"state": pr_state,
			"merged": pr_merged,
			"head": {"sha": pr_head_sha},
		}
		# `mergeable: null` is meaningful (mergeability still computing);
		# emit it explicitly so jq's `// ""` defaults fire.
		pr_response["mergeable"] = pr_mergeable  # type: ignore[assignment]

		# Check-runs response — the new check-runs gate in
		# merge_with_followup (round 12) fetches
		# /commits/{sha}/check-runs and refuses the merge if any
		# check-run is incomplete or has a non-success conclusion.
		# Default to one passing check-run so the gate clears; tests
		# that want to exercise the "checks pending" refusal pass
		# check_runs_state="pending".
		if check_runs_state == "pending":
			check_runs_response: dict = {
				"check_runs": [
					{"status": "in_progress", "conclusion": None, "name": "ci/test"},
				],
			}
		else:
			check_runs_response = {
				"check_runs": [
					{"status": "completed", "conclusion": "success", "name": "ci/test"},
				],
			}

		_mock_config: dict = {
			"api_responses": {
				"pulls/42": pr_response,
				f"commits/{pr_head_sha}/check-runs": check_runs_response,
			},
		}
		if issue_create_should_fail:
			_mock_config["issue_create_should_fail"] = True
		gh_state_file.write_text(
			json.dumps(_mock_config),
			encoding="utf-8",
		)

		labels_file = runtime_dir / "ensure_labels.txt"
		labels_file.write_text("", encoding="utf-8")
		github_output = runtime_dir / "github_output.txt"
		github_output.write_text("", encoding="utf-8")

		script_path = runtime_dir / "rb_branch_harness.sh"
		script_path.write_text(
			_build_merge_with_followup_harness(
				branch,
				github_output,
				enable_xpg_echo=enable_xpg_echo,
			),
			encoding="utf-8",
		)
		script_path.chmod(0o755)

		# Build judge JSON.  Empty title or body simulates the
		# "missing follow-up details" case the refusal gate catches.
		followup_payload: dict = {"title": followup_title, "body": followup_body}
		judge_json = json.dumps({
			"action": "merge_with_followup",
			"justification": "PR is shippable; deferred wiring tracked separately",
			"remaining_issues_summary": "deriveX has only test callers",
			"followup_issue": followup_payload,
		})

		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"ENSURE_LABELS_FILE": str(labels_file),
			"REPOSITORY": "owner/repo",
			"PR_NUMBER": "42",
			"ISSUE_NUMBERS": "41",
			"FIRST_ISSUE": "41",
			"FIRST_ISSUE_LABELS_JSON": json.dumps(parent_label_set),
			"JUDGE_JSON": judge_json,
			"RB_ACTION": "merge_with_followup",
			"ENABLE_AUTO_MERGE": enable_auto_merge,
			# Speed up both polling loops — one attempt is enough
			# because the mock returns the configured value
			# deterministically on the first call (sync merge succeeds
			# via the catch-all path in the mock).
			"PR_MERGEABLE_POLL_ATTEMPTS": "1",
			"PR_MERGEABLE_POLL_SLEEP": "0",
		}
		run_env = _sanitized_git_env(env)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=run_env,
			text=True,
			capture_output=True,
			timeout=60,
		)
		assert proc.returncode == 0, (
			f"merge_with_followup harness exited {proc.returncode}\n"
			f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		)

		state = json.loads(gh_state_file.read_text(encoding="utf-8"))
		state["_ensure_labels"] = labels_file.read_text(encoding="utf-8").strip().splitlines()
		state["_stdout"] = proc.stdout
		state["_github_output"] = github_output.read_text(encoding="utf-8")
		return state


def test_merge_with_followup_branch_gates_on_orchestrator_managed_label() -> None:
	"""Static check that mirrors the close_and_reissue branch contract:
	the propagation gate must be exactly `index("ai:orchestrator-managed")`
	against FIRST_ISSUE_LABELS_JSON.  Widening the gate would mislabel
	standalone follow-ups; narrowing or removing it would re-orphan
	orchestrator-managed lineage exactly like the bug the
	close_and_reissue tests already pin."""
	branch = _extract_merge_with_followup_branch()

	assert "FIRST_ISSUE_LABELS_JSON" in branch, (
		"merge_with_followup branch must reference FIRST_ISSUE_LABELS_JSON; "
		"otherwise it cannot decide whether to propagate the label."
	)
	assert 'jq -e \'index("ai:orchestrator-managed")\'' in branch, (
		"merge_with_followup branch must gate label propagation strictly "
		"on the parent carrying ai:orchestrator-managed."
	)
	assert 'RB_FOLLOWUP_LABELS+=("--label" "ai:orchestrator-managed")' in branch, (
		"merge_with_followup branch must append `--label ai:orchestrator-managed` "
		"to the gh issue create args via the RB_FOLLOWUP_LABELS array."
	)


def test_merge_with_followup_orchestrator_managed_parent_propagates_label() -> None:
	"""Parent carries ai:orchestrator-managed AND PR is mergeable AND
	follow-up details are present → follow-up issue must be created
	with `--label ai:orchestrator-managed` so the clarify auto-answer
	fast path fires on the new issue."""
	state = _run_merge_with_followup(
		parent_label_set=["ai:orchestrator-managed", "ai:review-blocked"],
	)

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1, (
		f"expected exactly one gh issue create call, got {len(creates)}: {creates}"
	)
	args = creates[0]
	assert "--label" in args, (
		f"follow-up issue must carry --label when parent is orchestrator-managed; "
		f"got args: {args}"
	)
	# Collect all (flag, value) label pairs — the script attaches both
	# `ai:clarification` (always, so the issue enters the pipeline
	# without waiting for clarify.yml's issues.opened handler) and
	# `ai:orchestrator-managed` (only when parent has it).
	label_values = [
		args[i + 1] for i, arg in enumerate(args)
		if arg == "--label" and i + 1 < len(args)
	]
	assert "ai:orchestrator-managed" in label_values, (
		f"follow-up must carry --label ai:orchestrator-managed when parent "
		f"is orchestrator-managed; got label values: {label_values}"
	)
	assert "ai:clarification" in label_values, (
		f"follow-up must carry --label ai:clarification so the issue enters "
		f"the pipeline immediately (matches the orchestrator path's pattern); "
		f"got label values: {label_values}"
	)
	assert "ai:orchestrator-managed" in state["_ensure_labels"], (
		"ensure_label_exists must be invoked for ai:orchestrator-managed "
		"before the gh issue create call; otherwise a fresh consumer repo "
		"could 422 on an unknown label."
	)
	assert "ai:clarification" in state["_ensure_labels"], (
		"ensure_label_exists must also be invoked for ai:clarification "
		"so a fresh consumer repo has the label defined before issue "
		"creation references it."
	)
	# Confirm the metadata footer landed in the issue body.
	idx = args.index("--body")
	body = args[idx + 1]
	assert "Merge-with-followup metadata" in body, (
		f"follow-up issue body must include the metadata footer; got body:\n{body}"
	)
	assert "Source PR: #42" in body, (
		"follow-up issue body must reference the source PR number so the "
		"deferred gap can be traced back to its origin"
	)
	assert "Parent issue: #41" in body, (
		"follow-up issue body must reference the parent linked issue"
	)
	# judge_handled must be emitted on the confirmed-merge path.
	assert "judge_handled=true" in state["_github_output"], (
		"merge_with_followup must emit judge_handled=true when merge is "
		"confirmed; otherwise the workflow's review-blocked fallback re-fires"
	)
	assert "judge_action=merge_with_followup" in state["_github_output"]


def test_merge_with_followup_standalone_parent_does_not_propagate_label() -> None:
	"""Parent does NOT carry ai:orchestrator-managed → follow-up must
	be created WITHOUT that label.  Standalone clarify semantics must
	be preserved (mirrors the close_and_reissue contract)."""
	state = _run_merge_with_followup(
		parent_label_set=["bug", "review-blocked"],
	)

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1
	args = creates[0]
	assert "ai:orchestrator-managed" not in args, (
		f"standalone follow-up must NOT carry ai:orchestrator-managed; "
		f"got args: {args}"
	)


def test_merge_with_followup_refuses_without_followup_details() -> None:
	"""Judge picked merge_with_followup but omitted follow-up
	title/body → action must be refused: NO gh issue create call,
	NO judge_handled=true emission.  This is the safety gate that
	prevents the new action from silently degrading to a plain merge
	(which would lose the deferred gap entirely)."""
	state = _run_merge_with_followup(
		parent_label_set=["ai:orchestrator-managed"],
		followup_title="",
		followup_body="",
	)

	creates = state.get("issue_create_args", [])
	assert creates == [], (
		f"merge_with_followup must NOT create any follow-up issue when "
		f"followup_issue is missing — that's the whole point of the safety "
		f"refusal. Got: {creates}"
	)
	assert "judge_handled=true" not in state["_github_output"], (
		"merge_with_followup must NOT emit judge_handled=true when refusing "
		"the action; the workflow's review-blocked fallback should fire instead"
	)


def test_merge_with_followup_skips_creation_when_pr_not_mergeable() -> None:
	"""PR has merge conflicts (mergeable=false) → MERGE_CONFIRMED stays
	false → NO follow-up issue created, NO label swap, NO
	judge_handled=true.  This prevents orphaning the tracking issue
	against code that never lands on the base ref."""
	state = _run_merge_with_followup(
		parent_label_set=["ai:orchestrator-managed"],
		pr_mergeable=False,
	)

	creates = state.get("issue_create_args", [])
	assert creates == [], (
		f"merge_with_followup must NOT create the follow-up issue when "
		f"the PR has merge conflicts (would orphan against unmerged code). "
		f"Got: {creates}"
	)
	assert "judge_handled=true" not in state["_github_output"], (
		"merge_with_followup must NOT emit judge_handled=true when merge "
		"cannot be confirmed; the PR must stay in ai:review-blocked for "
		"stall recovery"
	)


def test_merge_with_followup_skips_creation_when_auto_merge_disabled() -> None:
	"""PR is mergeable but ENABLE_AUTO_MERGE=false → operator wants to
	merge manually → MERGE_CONFIRMED stays false → NO follow-up issue
	created.  Avoids opening a tracking issue against a PR the
	operator might decide not to merge."""
	state = _run_merge_with_followup(
		parent_label_set=["ai:orchestrator-managed"],
		pr_mergeable=True,
		enable_auto_merge="false",
	)

	creates = state.get("issue_create_args", [])
	assert creates == [], (
		f"merge_with_followup must NOT create the follow-up issue when "
		f"auto-merge is disabled and the PR is still open — the operator "
		f"controls the merge decision. Got: {creates}"
	)
	assert "judge_handled=true" not in state["_github_output"]


def test_merge_with_followup_refuses_when_check_runs_pending() -> None:
	"""PR is mergeable=true but a check-run is still in_progress →
	the new check-runs gate must refuse the merge, leaving the issue
	in ai:review-blocked. Without this gate, sync merge could land
	code that subsequently fails informational CI (claude-branch-
	review consensus round 12)."""
	state = _run_merge_with_followup(
		parent_label_set=["ai:orchestrator-managed"],
		check_runs_state="pending",
	)

	creates = state.get("issue_create_args", [])
	assert creates == [], (
		f"merge_with_followup must NOT create the follow-up issue when "
		f"check-runs are still pending — the gate exists to prevent "
		f"creating a tracking issue against unvalidated code. Got: {creates}"
	)
	assert "judge_handled=true" not in state["_github_output"], (
		"merge_with_followup must NOT emit judge_handled=true when "
		"check-runs are pending; the PR must stay in ai:review-blocked "
		"for stall recovery to retry after checks complete"
	)


def test_merge_with_followup_creates_followup_when_pr_already_merged() -> None:
	"""PR was merged before the judge ran (rare race; e.g. a concurrent
	workflow merged it).  GitHub's REST `/pulls/{N}` reports this as
	``state=closed`` + ``merged=true`` — NEVER ``state=merged``.  The
	MERGE_CONFIRMED ladder must read `.merged` (boolean field), not
	infer "merged" from `.state`.  Pins the
	``PR_MERGED=true`` short-path of the ladder against the real GitHub
	API response shape; an earlier draft incorrectly checked
	``PR_STATE=merged`` and was unreachable in production."""
	state = _run_merge_with_followup(
		parent_label_set=["ai:orchestrator-managed"],
		pr_state="closed",  # GitHub's real shape for merged PRs
		pr_mergeable=None,
		pr_merged=True,  # the authoritative did-this-land signal
	)

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1, (
		f"merge_with_followup must create the follow-up issue when the PR "
		f"is already merged (.merged=true). Got: {creates}"
	)
	assert "judge_handled=true" in state["_github_output"]


def test_merge_with_followup_skips_when_pr_closed_without_merge() -> None:
	"""PR is closed but NOT merged (e.g. operator rejected and closed
	manually) → MERGE_CONFIRMED stays false → NO follow-up issue
	created.  Distinguishes the closed-without-merge case from the
	merged-and-closed case (both report ``state=closed``; only
	``.merged`` separates them).  Without this distinction, closing a
	PR while a merge_with_followup judge run is in flight would create
	a follow-up issue against a base ref that never received the PR's
	changes."""
	state = _run_merge_with_followup(
		parent_label_set=["ai:orchestrator-managed"],
		pr_state="closed",
		pr_mergeable=None,
		pr_merged=False,  # closed without merge
	)

	creates = state.get("issue_create_args", [])
	assert creates == [], (
		f"merge_with_followup must NOT create the follow-up when the PR "
		f"was closed without merge — the deferred gap would reference code "
		f"that never lands on the base ref. Got: {creates}"
	)
	assert "judge_handled=true" not in state["_github_output"]


def test_merge_with_followup_does_not_advance_when_issue_create_fails() -> None:
	"""``gh issue create`` fails (transient API / disabled-issues /
	permissions) AFTER merge confirmation → script must NOT swap the
	linked issue to ai:ready-to-merge and must NOT emit
	judge_handled=true.  The linked issue must stay in ai:review-
	blocked so stall recovery / the next judge run can retry follow-up
	creation; otherwise the PR is merged but the deferred gap is lost.
	Pins the order-of-operations fix where follow-up creation now
	happens BEFORE the label swap."""
	state = _run_merge_with_followup(
		parent_label_set=["ai:orchestrator-managed"],
		issue_create_should_fail=True,
	)

	# The script DID attempt to create the issue (gh issue create was
	# called once); we want to confirm it called it but recovered.
	creates = state.get("issue_create_args", [])
	assert len(creates) == 1, (
		f"merge_with_followup must attempt the issue create when merge "
		f"is confirmed. Got: {creates}"
	)
	# But after the failure, NO judge_handled emission.
	assert "judge_handled=true" not in state["_github_output"], (
		"merge_with_followup must NOT emit judge_handled=true when "
		"gh issue create failed; otherwise the workflow's review-blocked "
		"fallback is suppressed and the deferred gap is lost"
	)
	# And NO ai:ready-to-merge label swap (the label swap now happens
	# AFTER issue create succeeds — order-of-operations contract).
	ensure_labels = state["_ensure_labels"]
	assert "ai:ready-to-merge" not in ensure_labels, (
		f"ai:ready-to-merge must NOT be ensured when issue create failed "
		f"(label swap is gated on issue-create success). Got: {ensure_labels}"
	)


def test_merge_with_followup_preserves_judge_json_bytes_under_xpg_echo() -> None:
	state = _run_merge_with_followup(
		parent_label_set=["ai:orchestrator-managed"],
		followup_title='Follow-up: handle "quoted" wiring',
		followup_body='First line keeps "quotes" and literal \\n intact.\nSecond line stays attached.',
		enable_xpg_echo=True,
	)

	creates = state.get("issue_create_args", [])
	assert len(creates) == 1, (
		"merge_with_followup must still create the follow-up issue when bash xpg_echo is enabled"
	)
	args = creates[0]
	title = args[args.index("--title") + 1]
	body = args[args.index("--body") + 1]
	assert title == 'Follow-up: handle "quoted" wiring'
	assert body.startswith('First line keeps "quotes" and literal \\n intact.\nSecond line stays attached.')


def test_review_blocked_preamble_uses_printf_for_judge_json_extraction() -> None:
	src = _rb_judge_text()

	assert """RB_ACTION="$(printf '%s\\n' "${JUDGE_JSON}" | jq -r '.action')"
RB_JUSTIFICATION="$(printf '%s\\n' "${JUDGE_JSON}" | jq -r '.justification // "no justification"')"
RB_FIX_DESC="$(printf '%s\\n' "${JUDGE_JSON}" | jq -r '.fix_description // ""')"
RB_REMAINING="$(printf '%s\\n' "${JUDGE_JSON}" | jq -r '.remaining_issues_summary // ""')""" in src
	assert 'RB_ACTION="$(echo "${JUDGE_JSON}" | jq -r' not in src
	assert "FOLLOWUP_BODY=\"$(printf '%s\\n' \"${JUDGE_JSON}\" | jq -r '.followup_issue.body // empty')\"" in src
	assert "NEW_ISSUE_BODY=\"$(printf '%s\\n' \"${JUDGE_JSON}\" | jq -r '.new_issue.body // empty')\"" in src
	assert "| sed 's/\\\\n/\\n/g'" not in src


def test_review_blocked_pr_metadata_parsing_uses_printf_under_xpg_echo() -> None:
	src = _rb_judge_text()

	assert '_rps_new="$(printf \'%s\\n\' "${_rps_cur}" | jq -c --argjson p "${_rps_phases}" --arg t "${_rps_target}"' in src
	assert '_pr_state="$(printf \'%s\\n\' "${_pr_meta}" | jq -r \'.state // ""\')"' in src
	assert '_pr_merged="$(printf \'%s\\n\' "${_pr_meta}" | jq -r \'(.merged_at != null) or (.merged == true)\')"' in src
	assert '_guard_pr_merged="$(printf \'%s\\n\' "${_guard_pr_meta}" | jq -r \'(.merged_at != null) or (.merged == true)\')"' in src
	assert 'PR_STATE="$(printf \'%s\\n\' "${_pr_json}" | jq -r \'.state // ""\'' in src
	assert 'PR_MERGEABLE="$(printf \'%s\\n\' "${_pr_json}" | jq -r \'.mergeable // ""\'' in src
	assert 'PR_HEAD_SHA="$(printf \'%s\\n\' "${_pr_json}" | jq -r \'.head.sha // ""\'' in src
	assert 'PR_MERGED="$(printf \'%s\\n\' "${_pr_json}" | jq -r \'(.merged_at != null) or (.merged == true)\'' in src
	assert 'echo "${_rps_cur}" | jq -c' not in src
	assert 'echo "${_pr_meta}" | jq -r' not in src
	assert 'echo "${_guard_pr_meta}" | jq -r' not in src
	assert 'echo "${_pr_json}" | jq -r' not in src


# =============================================================================
# Merged-PR action guard coverage
# =============================================================================
#
# The early guard at script start allows merged PRs (state=closed +
# merged=true) through so the judge can pick merge_with_followup for
# the post-merge recovery flow. A merged-PR action guard later in the
# script refuses `fix` and `close_and_reissue` for merged PRs because
# those actions are structurally unsafe (fix would push to a merged
# branch; close_and_reissue would reissue work that already landed).
#
# Static checks because the guard sits between the JSON parse and the
# case statement, and exercising it end-to-end through the harness
# would require mocking the full script entry (including the early
# guard's gh api call) — significantly heavier than the
# close_and_reissue branch tests. The pattern checks below pin the
# guard's existence + structure so a future refactor cannot silently
# remove it.


def test_merged_pr_action_guard_refuses_fix_and_close_and_reissue() -> None:
	"""The guard must refuse fix and close_and_reissue for merged PRs.
	claude-branch-review Finding (round 4 Copilot): the early-guard
	permissiveness for merged PRs opens a footgun if the judge picks
	fix / close_and_reissue — both unsafe for code that has already
	landed. The guard pattern is asserted statically so a future
	refactor cannot silently drop it."""
	src = _rb_judge_text()

	# The guard must check PR_ALREADY_MERGED against the two safe actions.
	assert 'PR_ALREADY_MERGED' in src, (
		"review_rb_judge.sh must define PR_ALREADY_MERGED so the merged-PR "
		"action guard can refuse fix/close_and_reissue."
	)
	# Match the literal guard line (or fragments thereof) so widening
	# the safe-action set (e.g. accidentally allowing fix) is caught.
	assert '"${PR_ALREADY_MERGED:-false}" = "true"' in src, (
		"merged-PR action guard must use the canonical "
		'`[ \"${PR_ALREADY_MERGED:-false}\" = \"true\" ]` test.'
	)
	assert '[ "${RB_ACTION}" != "merge" ]' in src and '[ "${RB_ACTION}" != "merge_with_followup" ]' in src, (
		"merged-PR action guard must whitelist exactly 'merge' and "
		"'merge_with_followup' — widening the set would re-introduce "
		"the footgun the guard exists to prevent."
	)


def test_merged_pr_action_guard_emits_skip_reason() -> None:
	"""Refusal must emit `judge_skip_reason=merged_pr_unsafe_action` so
	downstream log analysis can classify the refusal. Without this,
	the workflow sees `judge_handled` unset with no explicit reason —
	indistinguishable from an unknown failure (claude-branch-review
	consensus Finding #2)."""
	src = _rb_judge_text()

	assert 'judge_skip_reason=merged_pr_unsafe_action' in src, (
		"merged-PR action guard must emit "
		"judge_skip_reason=merged_pr_unsafe_action so log analysis "
		"can classify the refusal explicitly."
	)
	# Pin the exit code so a refactor doesn't accidentally let the
	# script continue into the case dispatch after refusal.
	# The refusal block ends with `exit 0`.
	guard_match = re.search(
		r'judge_skip_reason=merged_pr_unsafe_action.*?exit 0',
		src,
		re.DOTALL,
	)
	assert guard_match is not None, (
		"merged-PR action guard must end with `exit 0` so the refused "
		"action does not continue into the case dispatch below."
	)


def test_merged_pr_action_guard_runs_before_judge_comment() -> None:
	"""The guard must run BEFORE the JUDGE_COMMENT post so a refused
	action does not leave a misleading audit trail on the PR
	(claude-branch-review consensus Finding #3 — comment claiming a
	`fix` or `close_and_reissue` action that the guard then silently
	refuses)."""
	src = _rb_judge_text()

	guard_pos = src.find('judge_skip_reason=merged_pr_unsafe_action')
	# JUDGE_COMMENT is the heredoc declaration; find its first
	# occurrence and ensure it sits AFTER the guard.
	comment_pos = src.find('JUDGE_COMMENT="## Review-Blocked Judge Decision')

	assert guard_pos > 0 and comment_pos > 0, (
		"failed to locate guard and JUDGE_COMMENT markers in "
		"review_rb_judge.sh — has the structure changed?"
	)
	assert guard_pos < comment_pos, (
		f"merged-PR action guard (pos={guard_pos}) must run BEFORE "
		f"the JUDGE_COMMENT post (pos={comment_pos}) so a refused "
		f"action does not leave a misleading audit trail on the PR."
	)


def test_review_rb_judge_validates_pr_number_before_pr_lookups() -> None:
	src = _rb_judge_text()
	guard = 'if [ -z "${PR_NUMBER:-}" ] || ! [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then'
	guard_pos = src.find(guard)
	lookup_pos = src.find('repos/${REPOSITORY}/pulls/${PR_NUMBER}')
	assert guard_pos != -1 and 'judge_skip_reason=invalid_pr_number' in src, (
		"review_rb_judge.sh must reject missing or non-numeric PR_NUMBER values before any PR-scoped API calls."
	)
	assert lookup_pos != -1 and guard_pos < lookup_pos, (
		"PR_NUMBER validation must run before review_rb_judge.sh performs PR-scoped lookups or path construction."
	)


def test_prompt_budget_helpers_fail_closed_on_non_numeric_counters() -> None:
	"""Prompt-budget helpers must pin their non-numeric-counter fallbacks.

	The shared primary helper (`gh_helpers.sh`) coerces both `_used` and
	`_size` to 0 before arithmetic. The narrower review-blocked-judge
	fallback still coerces `_used`, but it must omit the file entirely when
	`wc -c` returns a non-numeric `_size` so prompt budgeting cannot
	under-count a full-file embed.
	"""
	rb_src = _rb_judge_text()
	gh_src = (REPO_ROOT / "scripts" / "gh_helpers.sh").read_text(encoding="utf-8")
	for src, label in ((rb_src, "review_rb_judge.sh fallback"), (gh_src, "gh_helpers.sh primary")):
		assert '[[ "${_used}" =~ ^[0-9]+$ ]] || _used=0' in src, (
			f"{label} must coerce non-numeric _used values to 0 before arithmetic in _embed_input_file()."
		)
	assert '[[ "${_size}" =~ ^[0-9]+$ ]] || _size=0' in gh_src, (
		"gh_helpers.sh primary must coerce non-numeric _size values to 0 before integer comparisons in _embed_input_file()."
	)
	assert 'if ! [[ "${_size}" =~ ^[0-9]+$ ]]; then' in rb_src, (
		"review_rb_judge.sh fallback must guard non-numeric _size values before integer comparisons in _embed_input_file()."
	)
	assert 'printf \'(omitted — could not determine file size for prompt budgeting)\\n\'' in rb_src, (
		"review_rb_judge.sh fallback must omit files whose size probe is non-numeric so prompt budgeting cannot under-count a full embed."
	)


def main() -> int:
	# Direct `python3 tests/<file>.py` entrypoint — the repo's CI runs
	# tests via that pattern (see ci.yml) rather than pytest discovery,
	# so without this block the assertions never execute under CI.
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
