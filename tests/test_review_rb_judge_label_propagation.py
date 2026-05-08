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
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RB_JUDGE_SCRIPT = REPO_ROOT / "scripts" / "review_rb_judge.sh"


def _rb_judge_text() -> str:
	return RB_JUDGE_SCRIPT.read_text(encoding="utf-8")


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
	assert '--label" "ai:orchestrator-managed"' in branch or \
		'"--label" "ai:orchestrator-managed"' in branch, (
		"close_and_reissue branch must append `--label ai:orchestrator-managed` "
		"to the gh issue create args when the gate fires."
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
	repo = first_value("--repo") or "owner/repo"
	next_num = int(state.get("next_issue_number", 4301))
	state["next_issue_number"] = next_num + 1
	state.setdefault("issue_create_args", []).append(args)
	save()
	print(f"https://github.com/{repo}/issues/{next_num}")
	sys.exit(0)

if args[:2] == ["pr", "close"]:
	state.setdefault("pr_close_args", []).append(args)
	save()
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


def _build_harness(branch: str, runtime_dir: Path, github_output: Path) -> str:
	"""Wrap the extracted close_and_reissue branch in a self-contained
	bash harness with stubs for the helpers it depends on."""
	return f"""#!/usr/bin/env bash
set -euo pipefail

gh_retry() {{ "$@"; }}
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


def _run_close_and_reissue(parent_label_set: list[str]) -> dict:
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

		labels_file = runtime_dir / "ensure_labels.txt"
		labels_file.write_text("", encoding="utf-8")
		github_output = runtime_dir / "github_output.txt"
		github_output.write_text("", encoding="utf-8")

		script_path = runtime_dir / "rb_branch_harness.sh"
		script_path.write_text(
			_build_harness(branch, runtime_dir, github_output),
			encoding="utf-8",
		)
		script_path.chmod(0o755)

		judge_json = json.dumps({
			"action": "close_and_reissue",
			"justification": "rework needed",
			"new_issue": {
				"title": "Reissue: redo approach",
				"body": "Try again with smaller scope.",
			},
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
			"RB_ACTION": "close_and_reissue",
		}
		run_env = os.environ.copy()
		run_env.update(env)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
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
		run_env = os.environ.copy()
		run_env.update(env)

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
