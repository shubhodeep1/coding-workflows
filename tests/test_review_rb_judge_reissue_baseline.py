#!/usr/bin/env python3
"""Regression tests for review-blocked reissue baseline preservation.

Phase E adds an optional `spot-fix` reissue mode to
`scripts/review_rb_judge.sh` so close_and_reissue can preserve the
closed PR head on a new baseline branch instead of forcing a full redo.

These tests pin three contracts:
1. The judge prompt / workflow plumbing advertise the new additive fields.
2. `spot-fix` creates and pushes a baseline branch from the closed PR head,
   scopes the reissue body with `prior_pr_baseline_branch` + `files_touched`,
   and cleans up its temporary worktree without mutating the caller checkout.
3. Any invalid / disabled / failing spot-fix path degrades back to the
   existing redo behavior instead of aborting the reissue flow.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RB_JUDGE_SCRIPT = REPO_ROOT / "scripts" / "review_rb_judge.sh"
RB_JUDGE_PROMPT = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
REAL_GIT = shutil.which("git") or "git"


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	proc = subprocess.run(
		cmd,
		cwd=str(cwd) if cwd is not None else None,
		env=env,
		text=True,
		capture_output=True,
		timeout=90,
	)
	if proc.returncode != 0:
		raise AssertionError(
			f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
			f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		)
	return proc


def _rb_judge_text() -> str:
	return RB_JUDGE_SCRIPT.read_text(encoding="utf-8")


def _extract_close_and_reissue_branch() -> str:
	branch_lines: list[str] = []
	case_depth = 0
	inside_branch = False
	for line in _rb_judge_text().splitlines(keepends=True):
		stripped = line.strip()
		if not inside_branch:
			if stripped == "close_and_reissue)":
				inside_branch = True
			continue
		if stripped.startswith("case ") and stripped.endswith(" in"):
			case_depth += 1
		elif stripped == "esac" and case_depth > 0:
			case_depth -= 1
		elif stripped == ";;" and case_depth == 0:
			return "".join(branch_lines)
		branch_lines.append(line)
	raise AssertionError("could not extract close_and_reissue branch from review_rb_judge.sh")


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
	state.setdefault("issue_create_args", []).append(args)
	if state.get("issue_create_should_fail", False):
		save()
		sys.stderr.write("mock gh: simulated issue create failure\n")
		sys.exit(1)
	repo = first_value("--repo") or "owner/repo"
	next_num = int(state.get("next_issue_number", 5001))
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
	save()
	head_sha = state.get("pr_head_sha", "")
	jq_filter = first_value("--jq")
	if jq_filter == ".headRefOid":
		print(head_sha)
	else:
		print(json.dumps({"headRefOid": head_sha}))
	sys.exit(0)

if args[:2] == ["label", "create"]:
	state.setdefault("label_create_args", []).append(args)
	save()
	sys.exit(0)

save()
sys.exit(0)
'''
	gh_path = bin_dir / "gh"
	gh_path.write_text(gh_script, encoding="utf-8")
	gh_path.chmod(0o755)
	state_file.write_text("{}", encoding="utf-8")


def _install_git_wrapper(bin_dir: Path, state_file: Path) -> None:
	git_script = r'''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GIT_STATE_FILE"])
if state_path.exists():
	state = json.loads(state_path.read_text(encoding="utf-8"))
else:
	state = {}
args = sys.argv[1:]


def save() -> None:
	state_path.write_text(json.dumps(state), encoding="utf-8")


state.setdefault("calls", []).append(args)
if args[:1] == ["push"]:
	state.setdefault("push_args", []).append(args)
	needle = state.get("fail_push_branch_contains", "")
	if state.get("fail_push", False) and any(needle and needle in arg for arg in args):
		save()
		sys.stderr.write("mock git: simulated push failure\n")
		sys.exit(1)

save()
proc = subprocess.run([os.environ["REAL_GIT"], *args])
sys.exit(proc.returncode)
'''
	git_path = bin_dir / "git"
	git_path.write_text(git_script, encoding="utf-8")
	git_path.chmod(0o755)
	state_file.write_text(json.dumps({"fail_push": False}), encoding="utf-8")


def _build_harness(branch: str, github_output: Path) -> str:
	return f"""#!/usr/bin/env bash
set -euo pipefail

gh_retry() {{ "$@"; }}
ensure_label_exists() {{ printf '%s\\n' "$1" >> "${{ENSURE_LABELS_FILE}}"; }}
_resilient_phase_swap() {{ :; }}
sleep() {{ :; }}

GITHUB_OUTPUT="{github_output}"

case "${{RB_ACTION}}" in
  close_and_reissue)
{branch}
    ;;
esac
"""


def _create_git_fixture(root: Path) -> dict[str, str]:
	origin = root / "origin.git"
	seed = root / "seed"
	judge = root / "judge"

	_run([REAL_GIT, "init", "--bare", str(origin)])
	_run([REAL_GIT, "init", str(seed)])
	_run([REAL_GIT, "-C", str(seed), "config", "user.name", "Test User"])
	_run([REAL_GIT, "-C", str(seed), "config", "user.email", "test@example.com"])
	(seed / "tracked.txt").write_text("base\n", encoding="utf-8")
	_run([REAL_GIT, "-C", str(seed), "add", "tracked.txt"])
	_run([REAL_GIT, "-C", str(seed), "commit", "-m", "base"])
	_run([REAL_GIT, "-C", str(seed), "branch", "-M", "main"])
	_run([REAL_GIT, "-C", str(seed), "remote", "add", "origin", str(origin)])
	_run([REAL_GIT, "-C", str(seed), "push", "-u", "origin", "main"])
	_run([REAL_GIT, "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"])

	_run([REAL_GIT, "-C", str(seed), "checkout", "-b", "pr-source"])
	(seed / "tracked.txt").write_text("base\npr change\n", encoding="utf-8")
	(seed / "other.txt").write_text("from pr\n", encoding="utf-8")
	_run([REAL_GIT, "-C", str(seed), "add", "tracked.txt", "other.txt"])
	_run([REAL_GIT, "-C", str(seed), "commit", "-m", "pr head"])
	pr_head_sha = _run([REAL_GIT, "-C", str(seed), "rev-parse", "HEAD"]).stdout.strip()
	_run([REAL_GIT, "-C", str(seed), "push", "-u", "origin", "pr-source"])

	_run([REAL_GIT, "clone", str(origin), str(judge)])
	_run([REAL_GIT, "-C", str(judge), "config", "user.name", "Judge User"])
	_run([REAL_GIT, "-C", str(judge), "config", "user.email", "judge@example.com"])
	return {
		"origin": str(origin),
		"seed": str(seed),
		"judge": str(judge),
		"pr_head_sha": pr_head_sha,
	}


def _arg_value(args: list[str], flag: str) -> str:
	for i, arg in enumerate(args):
		if arg == flag and i + 1 < len(args):
			return args[i + 1]
	return ""


def _run_close_and_reissue(
	*,
	reissue_mode: str = "spot-fix",
	remaining_issues: list[dict[str, object]] | None = None,
	feature_flag: str = "true",
	fail_push: bool = False,
) -> dict[str, object]:
	branch = _extract_close_and_reissue_branch()
	remaining = list(remaining_issues or [])

	with tempfile.TemporaryDirectory(prefix="test_rb_reissue_baseline_") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		bin_dir = tmp_path / "bin"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		bin_dir.mkdir(parents=True, exist_ok=True)

		fixture = _create_git_fixture(tmp_path)
		judge_repo = Path(fixture["judge"])
		origin_repo = fixture["origin"]
		pr_head_sha = fixture["pr_head_sha"]
		expected_branch = f"ai/reissue-pr-42-baseline-{pr_head_sha[:12]}"

		gh_state_file = runtime_dir / "gh_state.json"
		git_state_file = runtime_dir / "git_state.json"
		_install_mock_gh(bin_dir, gh_state_file)
		_install_git_wrapper(bin_dir, git_state_file)

		gh_state_file.write_text(json.dumps({"pr_head_sha": pr_head_sha}), encoding="utf-8")
		git_state_file.write_text(
			json.dumps(
				{
					"fail_push": fail_push,
					"fail_push_branch_contains": expected_branch,
				}
			),
			encoding="utf-8",
		)

		labels_file = runtime_dir / "ensure_labels.txt"
		labels_file.write_text("", encoding="utf-8")
		github_output = runtime_dir / "github_output.txt"
		github_output.write_text("", encoding="utf-8")

		script_path = runtime_dir / "rb_branch_harness.sh"
		script_path.write_text(_build_harness(branch, github_output), encoding="utf-8")
		script_path.chmod(0o755)

		judge_json = json.dumps(
			{
				"action": "close_and_reissue",
				"reissue_mode": reissue_mode,
				"justification": "rework needed",
				"remaining_issues_summary": "localized issues remain",
				"remaining_issues": remaining,
				"new_issue": {
					"title": "Reissue: surgical follow-up",
					"body": "Fix the remaining defects only.",
				},
			}
		)

		start_head = _run([REAL_GIT, "-C", str(judge_repo), "rev-parse", "HEAD"]).stdout.strip()
		start_status = _run([REAL_GIT, "-C", str(judge_repo), "status", "--porcelain"]).stdout
		start_user_name = _run([REAL_GIT, "-C", str(judge_repo), "config", "user.name"]).stdout.strip()
		start_user_email = _run([REAL_GIT, "-C", str(judge_repo), "config", "user.email"]).stdout.strip()

		env = os.environ.copy()
		env.update(
			{
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
				"REAL_GIT": REAL_GIT,
				"MOCK_GH_STATE_FILE": str(gh_state_file),
				"MOCK_GIT_STATE_FILE": str(git_state_file),
				"ENSURE_LABELS_FILE": str(labels_file),
				"REPOSITORY": "owner/repo",
				"PR_NUMBER": "42",
				"ISSUE_NUMBERS": "41",
				"FIRST_ISSUE": "41",
				"FIRST_ISSUE_LABELS_JSON": json.dumps(["ai:orchestrator-managed", "ai:closed"]),
				"JUDGE_JSON": judge_json,
				"RB_ACTION": "close_and_reissue",
				"REISSUE_PRESERVE_BASELINE_ENABLED": feature_flag,
				"RUNTIME_DIR": str(runtime_dir),
			}
		)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(judge_repo),
			env=env,
			text=True,
			capture_output=True,
			timeout=90,
		)
		if proc.returncode != 0:
			raise AssertionError(
				f"harness exited {proc.returncode}\nstdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
			)

		end_head = _run([REAL_GIT, "-C", str(judge_repo), "rev-parse", "HEAD"]).stdout.strip()
		end_status = _run([REAL_GIT, "-C", str(judge_repo), "status", "--porcelain"]).stdout
		end_user_name = _run([REAL_GIT, "-C", str(judge_repo), "config", "user.name"]).stdout.strip()
		end_user_email = _run([REAL_GIT, "-C", str(judge_repo), "config", "user.email"]).stdout.strip()
		worktree_dirs = sorted(p.name for p in runtime_dir.glob("review-rb-reissue-wt-*"))

		gh_state = json.loads(gh_state_file.read_text(encoding="utf-8"))
		git_state = json.loads(git_state_file.read_text(encoding="utf-8"))

		result: dict[str, object] = {
			"stdout": proc.stdout,
			"stderr": proc.stderr,
			"gh_state": gh_state,
			"git_state": git_state,
			"github_output": github_output.read_text(encoding="utf-8"),
			"ensure_labels": labels_file.read_text(encoding="utf-8").splitlines(),
			"start_head": start_head,
			"end_head": end_head,
			"start_status": start_status,
			"end_status": end_status,
			"start_user_name": start_user_name,
			"end_user_name": end_user_name,
			"start_user_email": start_user_email,
			"end_user_email": end_user_email,
			"worktree_dirs": worktree_dirs,
			"expected_branch": expected_branch,
			"origin": origin_repo,
			"pr_head_sha": pr_head_sha,
		}

		creates = gh_state.get("issue_create_args", [])
		result["issue_create_args"] = creates
		result["issue_body"] = _arg_value(creates[0], "--body") if creates else ""

		ls_remote = subprocess.run(
			[REAL_GIT, "ls-remote", origin_repo, f"refs/heads/{expected_branch}"],
			text=True,
			capture_output=True,
			timeout=30,
		)
		result["baseline_remote_ref"] = ls_remote.stdout.strip()
		return result


def test_prompt_contract_includes_reissue_mode_and_remaining_issues() -> None:
	prompt = RB_JUDGE_PROMPT.read_text(encoding="utf-8")
	assert '"reissue_mode": "spot-fix" | "redo" | ""' in prompt, (
		"mode-judge-review-blocked.txt must advertise the additive reissue_mode field"
	)
	assert '"remaining_issues": [' in prompt, (
		"mode-judge-review-blocked.txt must advertise the structured remaining_issues array"
	)
	assert 'choose a `reissue_mode`' in prompt, (
		"mode-judge-review-blocked.txt must explain when to choose spot-fix vs redo"
	)


def test_review_autofix_exports_reissue_preserve_baseline_flag() -> None:
	src = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")
	assert 'REISSUE_PRESERVE_BASELINE_ENABLED: ${{ vars.REISSUE_PRESERVE_BASELINE_ENABLED || \'false\' }}' in src, (
		"review_autofix.yml must plumb REISSUE_PRESERVE_BASELINE_ENABLED with a false default"
	)


def test_implement_workflow_has_prior_pr_baseline_branch_checkout_override() -> None:
	src = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	assert 'prior_pr_baseline_branch:' in src, (
		"implement.yml must parse prior_pr_baseline_branch from the issue body"
	)
	assert 'Checkout prior PR baseline branch' in src, (
		"implement.yml must attempt a baseline-branch checkout before the default checkout"
	)
	assert 'matches = re.findall' in src and 'matches[-1] if matches else ""' in src, (
		"implement.yml must prefer the last prior_pr_baseline_branch match so appended metadata wins"
	)
	assert "steps.checkout_ref.outputs.baseline_branch == '' || steps.checkout_baseline.outcome != 'success'" in src, (
		"implement.yml must fail open to the integration/default checkout when the baseline branch is absent or uncheckoutable"
	)


def test_spot_fix_reissue_preserves_baseline_branch_and_cleans_up_worktree() -> None:
	result = _run_close_and_reissue(
		remaining_issues=[
			{"file": "tracked.txt", "line_start": 1, "line_end": 2, "symptom": "needs surgical fix"},
			{"file": "other.txt", "line_start": 1, "line_end": 1, "symptom": "wire missing caller"},
			{"file": "tracked.txt", "line_start": 3, "line_end": 3, "symptom": "duplicate path dedupe"},
		],
	)

	issue_body = str(result["issue_body"])
	assert "REISSUE_BASELINE_PRESERVED" in str(result["stdout"]), (
		"spot-fix happy path must log REISSUE_BASELINE_PRESERVED"
	)
	assert "REISSUE_MODE spot-fix" in str(result["stdout"]), (
		"spot-fix happy path must log REISSUE_MODE spot-fix"
	)
	assert f"prior_pr_baseline_branch: {result['expected_branch']}" in issue_body, (
		f"replacement issue body must carry prior_pr_baseline_branch. Got:\n{issue_body}"
	)
	assert "files_touched:" in issue_body, (
		f"replacement issue body must carry files_touched when spot-fix succeeds. Got:\n{issue_body}"
	)
	assert "- tracked.txt" in issue_body and "- other.txt" in issue_body, (
		f"files_touched must include the deduped remaining_issues[].file paths. Got:\n{issue_body}"
	)
	assert issue_body.count("- tracked.txt") == 1, (
		f"files_touched must dedupe duplicate file paths. Got:\n{issue_body}"
	)
	assert str(result["baseline_remote_ref"]).startswith(str(result["pr_head_sha"])), (
		"spot-fix must push a remote baseline branch at the closed PR head SHA"
	)
	assert result["start_head"] == result["end_head"], (
		"spot-fix must not move the caller checkout HEAD; only the throwaway worktree may change"
	)
	assert result["start_status"] == result["end_status"] == "", (
		"spot-fix must leave the caller checkout clean"
	)
	assert result["start_user_name"] == result["end_user_name"] == "Judge User", (
		"spot-fix must not overwrite the caller checkout git user.name"
	)
	assert result["start_user_email"] == result["end_user_email"] == "judge@example.com", (
		"spot-fix must not overwrite the caller checkout git user.email"
	)
	assert result["worktree_dirs"] == [], (
		f"spot-fix must clean up the temporary worktree via EXIT trap; found leftovers: {result['worktree_dirs']}"
	)


def test_spot_fix_feature_flag_off_falls_back_to_redo() -> None:
	result = _run_close_and_reissue(
		feature_flag="false",
		remaining_issues=[
			{"file": "tracked.txt", "line_start": 1, "line_end": 2, "symptom": "localized issue"},
		],
	)

	issue_body = str(result["issue_body"])
	gh_state = result["gh_state"]
	assert "REISSUE_BASELINE_DISCARDED requested=spot-fix reason=disabled" in str(result["stdout"]), (
		"feature-flag-off path must log REISSUE_BASELINE_DISCARDED"
	)
	assert "REISSUE_MODE redo" in str(result["stdout"]), (
		"feature-flag-off path must degrade to redo"
	)
	assert gh_state.get("pr_view_args", []) == [], (
		"feature-flag-off redo path must not query the PR head SHA or attempt baseline creation"
	)
	assert "prior_pr_baseline_branch:" not in issue_body and "files_touched:" not in issue_body, (
		f"redo fallback must not add baseline metadata to the new issue body. Got:\n{issue_body}"
	)
	assert result["baseline_remote_ref"] == "", (
		"feature-flag-off redo path must not push a baseline branch"
	)


def test_spot_fix_empty_remaining_issues_falls_back_to_redo() -> None:
	result = _run_close_and_reissue(remaining_issues=[])

	assert "REISSUE_BASELINE_DISCARDED requested=spot-fix reason=empty_remaining_issues" in str(result["stdout"]), (
		"empty remaining_issues must degrade to redo with an explicit discard log"
	)
	assert "REISSUE_MODE redo" in str(result["stdout"]), (
		"empty remaining_issues fallback must log redo as the effective mode"
	)
	assert result["baseline_remote_ref"] == "", (
		"empty remaining_issues fallback must not push a baseline branch"
	)


def test_spot_fix_push_failure_falls_back_to_redo() -> None:
	result = _run_close_and_reissue(
		fail_push=True,
		remaining_issues=[
			{"file": "tracked.txt", "line_start": 1, "line_end": 2, "symptom": "localized issue"},
		],
	)

	issue_body = str(result["issue_body"])
	assert "REISSUE_BASELINE_DISCARDED requested=spot-fix reason=baseline_prepare_failed" in str(result["stdout"]), (
		"push failure must fail open to redo and emit REISSUE_BASELINE_DISCARDED"
	)
	assert "REISSUE_MODE redo" in str(result["stdout"]), (
		"push failure fallback must log redo as the effective mode"
	)
	assert "prior_pr_baseline_branch:" not in issue_body and "files_touched:" not in issue_body, (
		f"push failure fallback must not leave stale baseline metadata in the reissue body. Got:\n{issue_body}"
	)
	assert result["baseline_remote_ref"] == "", (
		"push failure fallback must not leave a remote baseline branch behind"
	)


def main() -> int:
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
