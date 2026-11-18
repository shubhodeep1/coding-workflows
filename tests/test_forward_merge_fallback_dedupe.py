"""forward-merge-stable-to-main.yml must not open a second PR for one stable tip.

A release pushes to `stable`, which starts the workflow, and
test-and-mark-stable.yml's sync-to-main job dispatches it again for the same
commit. Both runs opened a fallback PR (#8169 and #8170). Each resolved the
same conflict separately; after #8169 merged, #8170 had two merge bases with
main, stayed conflicted on GitHub and never merged.

These tests run the real "Fallback — open PR for manual merge" step body
against a local origin, with a `gh` stub that applies the step's own --jq
filter through jq.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

# The gh stub evaluates the step's --jq filter with system jq.
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq required")

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "forward-merge-stable-to-main.yml"
STEP_NAME = "Fallback — open PR for manual merge"

GH_STUB = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["GH_CALL_LOG"], "a", encoding="utf-8") as fh:
	fh.write(json.dumps(args) + "\n")
if args[:1] == ["api"]:
	if os.environ.get("GH_STUB_API_FAIL") == "1":
		sys.stderr.write("HTTP 502\n")
		sys.exit(1)
	jq = args[args.index("--jq") + 1]
	out = subprocess.run(["jq", "-r", jq], input=os.environ["GH_STUB_PULLS"], text=True, capture_output=True, check=True)
	sys.stdout.write(out.stdout)
	sys.exit(0)
if args[:2] == ["pr", "create"]:
	print("https://github.com/o/r/pull/9001")
	sys.exit(0)
sys.exit(0)
"""


def _env() -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "BASH_ENV"}
	env.update(
		{
			"GIT_AUTHOR_NAME": "t",
			"GIT_AUTHOR_EMAIL": "t@example.com",
			"GIT_COMMITTER_NAME": "t",
			"GIT_COMMITTER_EMAIL": "t@example.com",
			"GIT_CONFIG_NOSYSTEM": "1",
		}
	)
	return env


def _git(repo: Path, *args: str) -> str:
	return subprocess.run(["git", *args], cwd=repo, env=_env(), check=True, text=True, capture_output=True).stdout.strip()


def _step_body() -> str:
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	for step in workflow["jobs"]["forward-merge"]["steps"]:
		if step.get("name") == STEP_NAME:
			assert "${{" not in step["run"], "the fallback body must read inputs from env:, not template expressions"
			return step["run"]
	raise AssertionError(f"step {STEP_NAME!r} not found")


def _setup(tmp: Path, *, existing_has_tip: bool) -> Path:
	origin = tmp / "origin.git"
	subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=_env(), check=True)
	work = tmp / "work"
	work.mkdir()
	_git(work, "init", "-q", "-b", "main")
	(work / "f").write_text("base\n", encoding="utf-8")
	_git(work, "add", "f")
	_git(work, "commit", "-qm", "base")
	_git(work, "checkout", "-qb", "stable")
	(work / "g").write_text("fix 1\n", encoding="utf-8")
	_git(work, "add", "g")
	_git(work, "commit", "-qm", "stable fix 1")
	# The earlier run's fallback branch, cut from stable at that time.
	_git(work, "branch", "auto/forward-merge-stable-100-1")
	if not existing_has_tip:
		(work / "g").write_text("fix 2\n", encoding="utf-8")
		_git(work, "commit", "-qam", "stable fix 2")
	_git(work, "push", "-q", str(origin), "main", "stable", "auto/forward-merge-stable-100-1")
	checkout = tmp / "checkout"
	subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(checkout)], env=_env(), check=True)
	_git(checkout, "fetch", "-q", "origin", "stable", "main")
	return checkout


def _run(checkout: Path, tmp: Path, *, pulls: list[dict], api_fail: bool = False) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict[str, str]]:
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(GH_STUB, encoding="utf-8")
	gh.chmod(0o755)
	call_log = tmp / "gh_calls.jsonl"
	call_log.write_text("", encoding="utf-8")
	output = tmp / "github_output"
	output.write_text("", encoding="utf-8")
	env = _env()
	env.update(
		{
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"GH_CALL_LOG": str(call_log),
			"GH_STUB_PULLS": json.dumps(pulls),
			"GH_STUB_API_FAIL": "1" if api_fail else "0",
			"GITHUB_OUTPUT": str(output),
			"GITHUB_REPOSITORY": "o/r",
			"GITHUB_RUN_ID": "200",
			"GITHUB_RUN_ATTEMPT": "1",
			"GITHUB_SHA": _git(checkout, "rev-parse", "origin/stable"),
			"MERGE_STATUS": "conflict",
			"CONFLICT_FILES": "f",
		}
	)
	proc = subprocess.run(["bash", "-e", "-c", _step_body()], cwd=checkout, env=env, text=True, capture_output=True, timeout=120, check=False)
	calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines() if line.strip()]
	outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if "=" in line)
	return proc, calls, outputs


def _open_pr(number: int, ref: str, repo: str = "o/r") -> dict:
	return {"number": number, "head": {"ref": ref, "repo": {"full_name": repo}}}


def _remote_branches(checkout: Path) -> list[str]:
	return sorted(line.split("refs/heads/", 1)[1] for line in _git(checkout, "ls-remote", "--heads", "origin").splitlines())


def test_open_pr_carrying_the_stable_tip_is_reused() -> None:
	with tempfile.TemporaryDirectory(prefix="fwd_merge_dedupe_") as td:
		tmp = Path(td)
		checkout = _setup(tmp, existing_has_tip=True)
		pulls = [_open_pr(8169, "auto/forward-merge-stable-100-1"), _open_pr(7, "claude/unrelated")]
		proc, calls, outputs = _run(checkout, tmp, pulls=pulls)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "FORWARD_MERGE_FALLBACK outcome=reused pr=8169 head_ref=auto/forward-merge-stable-100-1" in proc.stdout
		assert outputs.get("reused_pr_number") == "8169"
		assert "pr_number" not in outputs and "pr_url" not in outputs
		assert not any(call[:2] == ["pr", "create"] for call in calls), calls
		assert "auto/forward-merge-stable-200-1" not in _remote_branches(checkout)
		api_calls = [call for call in calls if call[:1] == ["api"]]
		assert len(api_calls) == 1
		assert api_calls[0][1] == "repos/o/r/pulls?state=open&base=main&per_page=100"
		assert "--paginate" in api_calls[0]


def test_fork_pr_with_a_matching_ref_name_is_not_reused() -> None:
	with tempfile.TemporaryDirectory(prefix="fwd_merge_dedupe_") as td:
		tmp = Path(td)
		checkout = _setup(tmp, existing_has_tip=True)
		pulls = [_open_pr(8169, "auto/forward-merge-stable-100-1", repo="fork/r")]
		proc, calls, outputs = _run(checkout, tmp, pulls=pulls)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "FORWARD_MERGE_FALLBACK outcome=open_new" in proc.stdout
		assert "reused_pr_number" not in outputs
		assert any(call[:2] == ["pr", "create"] for call in calls), calls
		assert outputs.get("pr_number") == "9001"


def test_open_pr_for_an_older_stable_tip_does_not_block_a_new_pr() -> None:
	with tempfile.TemporaryDirectory(prefix="fwd_merge_dedupe_") as td:
		tmp = Path(td)
		checkout = _setup(tmp, existing_has_tip=False)
		proc, calls, outputs = _run(checkout, tmp, pulls=[_open_pr(8169, "auto/forward-merge-stable-100-1")])
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "FORWARD_MERGE_FALLBACK outcome=open_new" in proc.stdout
		assert any(call[:2] == ["pr", "create"] for call in calls), calls
		assert outputs.get("pr_number") == "9001"
		assert "reused_pr_number" not in outputs
		assert "auto/forward-merge-stable-200-1" in _remote_branches(checkout)


def test_listing_failure_fails_open_to_a_new_pr() -> None:
	with tempfile.TemporaryDirectory(prefix="fwd_merge_dedupe_") as td:
		tmp = Path(td)
		checkout = _setup(tmp, existing_has_tip=True)
		proc, calls, outputs = _run(checkout, tmp, pulls=[], api_fail=True)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "Could not list open forward-merge PRs" in proc.stdout
		assert any(call[:2] == ["pr", "create"] for call in calls), calls
		assert outputs.get("pr_number") == "9001"


def test_no_open_fallback_pr_opens_a_new_one() -> None:
	with tempfile.TemporaryDirectory(prefix="fwd_merge_dedupe_") as td:
		tmp = Path(td)
		checkout = _setup(tmp, existing_has_tip=True)
		proc, _calls, outputs = _run(checkout, tmp, pulls=[_open_pr(7, "claude/unrelated")])
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "FORWARD_MERGE_FALLBACK outcome=open_new" in proc.stdout
		assert outputs.get("pr_number") == "9001"
