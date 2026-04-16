#!/usr/bin/env python3
"""Function-level tests for probe_sibling_merge_conflicts() in
scripts/orchestrate_poll_process.sh.

Rather than wiring into the end-to-end poller harness (which requires
substantial fixture setup for a ready-to-merge flow), these tests
extract just the merge-probe helper functions from
orchestrate_poll_process.sh and execute them against a real throwaway
git repository with a stub `gh` CLI on PATH. This gives byte-level
coverage of the git-merge-tree probe behavior without running the
full poller.

The extraction uses bash's built-in function-text support: the test
script echoes the function definitions into a file using awk and
sources that file. No parsing or regex-rewriting of the production
script happens here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	"""Run a bash script with a controlled env and return the result."""
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=full_env,
		capture_output=True,
		text=True,
	)


def _init_git_repo(repo_dir: Path) -> tuple[str, str, str]:
	"""Create a minimal repo with two diverging branches and return
	(integration_sha, candidate_sha, sibling_sha)."""
	repo_dir.mkdir(parents=True, exist_ok=True)
	g = ["git", "-C", str(repo_dir)]
	subprocess.run(g + ["init", "--quiet", "--initial-branch=main"], check=True)
	subprocess.run(g + ["config", "user.email", "test@example.com"], check=True)
	subprocess.run(g + ["config", "user.name", "Test"], check=True)
	# Disable signing — the test doesn't need real signatures, and
	# global signing config enforced by the dev environment wrapper
	# would otherwise fail every commit with "missing source".
	subprocess.run(g + ["config", "commit.gpgsign", "false"], check=True)
	subprocess.run(g + ["config", "tag.gpgsign", "false"], check=True)
	subprocess.run(g + ["config", "gpg.format", "openpgp"], check=True)
	subprocess.run(g + ["config", "user.signingkey", ""], check=False)
	# Base commit with a shared file.
	(repo_dir / "README.md").write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
	subprocess.run(g + ["add", "README.md"], check=True)
	subprocess.run(g + ["commit", "--quiet", "-m", "base"], check=True)
	integration_sha = subprocess.run(
		g + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True
	).stdout.strip()
	# Create a fake remote so `origin/<branch>` refs work: we'll use
	# refs/remotes/origin/<branch> directly via update-ref.
	subprocess.run(g + ["update-ref", "refs/remotes/origin/integration", integration_sha], check=True)

	# Candidate branch: rewrites line 2 on README.md.
	subprocess.run(g + ["checkout", "--quiet", "-b", "candidate"], check=True)
	(repo_dir / "README.md").write_text("line 1\nCANDIDATE\nline 3\n", encoding="utf-8")
	subprocess.run(g + ["commit", "--quiet", "-am", "candidate edit"], check=True)
	candidate_sha = subprocess.run(
		g + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True
	).stdout.strip()
	subprocess.run(g + ["update-ref", "refs/remotes/origin/candidate", candidate_sha], check=True)

	# Sibling branch that conflicts: also rewrites line 2 differently.
	subprocess.run(g + ["checkout", "--quiet", "main"], check=True)
	subprocess.run(g + ["checkout", "--quiet", "-b", "sibling-conflict"], check=True)
	(repo_dir / "README.md").write_text("line 1\nSIBLING-CONFLICT\nline 3\n", encoding="utf-8")
	subprocess.run(g + ["commit", "--quiet", "-am", "sibling conflicting edit"], check=True)
	sibling_conflict_sha = subprocess.run(
		g + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True
	).stdout.strip()
	subprocess.run(g + ["update-ref", "refs/remotes/origin/sibling-conflict", sibling_conflict_sha], check=True)

	# Sibling branch that is clean: adds an unrelated file.
	subprocess.run(g + ["checkout", "--quiet", "main"], check=True)
	subprocess.run(g + ["checkout", "--quiet", "-b", "sibling-clean"], check=True)
	(repo_dir / "unrelated.txt").write_text("hello\n", encoding="utf-8")
	subprocess.run(g + ["add", "unrelated.txt"], check=True)
	subprocess.run(g + ["commit", "--quiet", "-m", "unrelated add"], check=True)
	sibling_clean_sha = subprocess.run(
		g + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True
	).stdout.strip()
	subprocess.run(g + ["update-ref", "refs/remotes/origin/sibling-clean", sibling_clean_sha], check=True)

	return integration_sha, candidate_sha, sibling_conflict_sha, sibling_clean_sha


PROBE_EXTRACT_SCRIPT = r'''
set -euo pipefail

# Extract probe_sibling_merge_conflicts + _merge_probe_refresh +
# _bump_merge_deferral_count from the production script so we can
# exercise them in isolation. Uses awk: slurps lines between the
# function name and its terminating brace (first column '}').
extract_fn() {
	local fn="$1"
	awk -v fn="${fn}" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^}$/ { exit }
	' "'"${POLLER_SCRIPT}"'"
}

# Also extract the MAX_MERGE_DEFERRALS default block.
extract_max_defer() {
	awk '
		/^MAX_MERGE_DEFERRALS=/,/^fi$/ { print; if ($0 == "fi") exit }
	' "'"${POLLER_SCRIPT}"'"
}

mkdir -p extracted
: > extracted/probe.sh
extract_max_defer >> extracted/probe.sh
echo '' >> extracted/probe.sh
# Declare globals the extracted functions rely on.
echo 'declare -gA _MERGE_PROBE_CACHE_JSON=()' >> extracted/probe.sh
echo 'declare -gA _MERGE_PROBE_CACHE_FETCHED=()' >> extracted/probe.sh
extract_fn '_merge_probe_refresh' >> extracted/probe.sh
echo '' >> extracted/probe.sh
extract_fn 'probe_sibling_merge_conflicts' >> extracted/probe.sh
echo '' >> extracted/probe.sh
extract_fn '_bump_merge_deferral_count' >> extracted/probe.sh
'''


def _extract_probe_functions(repo_dir: Path) -> None:
	"""Run the extractor and leave the sourceable file at extracted/probe.sh."""
	script = PROBE_EXTRACT_SCRIPT.replace('"\'"${POLLER_SCRIPT}"\'"', f'"{POLLER_SCRIPT}"')
	r = _run_bash(script, cwd=repo_dir)
	if r.returncode != 0:
		raise AssertionError(f"probe extraction failed: {r.stderr}\nstdout={r.stdout}")
	out = repo_dir / "extracted" / "probe.sh"
	if not out.exists() or out.stat().st_size == 0:
		raise AssertionError(f"probe.sh not extracted; dir contents: {list((repo_dir / 'extracted').iterdir())}")


def _mock_gh_script() -> str:
	"""Return a bash stub that shadows `gh` so `gh pr list` returns a
	caller-controlled JSON array (from $GH_MOCK_PR_LIST_JSON)."""
	return textwrap.dedent(r"""
	gh() {
		if [ "${1:-}" = "pr" ] && [ "${2:-}" = "list" ]; then
			printf '%s' "${GH_MOCK_PR_LIST_JSON:-[]}"
			return 0
		fi
		return 0
	}
	gh_retry() { "$@"; }
	_safe_gh_jq() { return 0; }
	tg_notify() { return 0; }
	tg_notify_issue() { return 0; }
	export -f gh gh_retry _safe_gh_jq tg_notify tg_notify_issue 2>/dev/null || true
	""")


def test_probe_detects_conflict_with_sibling(tmp_path=None):
	"""The probe returns 1 and logs the conflict paths when a sibling PR
	touches the same line in the same file as the candidate."""
	import tempfile
	with tempfile.TemporaryDirectory(prefix="merge-probe-") as td:
		repo_dir = Path(td)
		_init_git_repo(repo_dir)
		_extract_probe_functions(repo_dir)

		sibling_list = json.dumps([
			{"number": 701, "headRefName": "sibling-conflict", "headRefOid": "deadbeef"}
		])
		script = _mock_gh_script() + textwrap.dedent(r"""
		source extracted/probe.sh
		if probe_sibling_merge_conflicts "700" "candidate" "integration"; then
			echo "PROBE_RC=0"
		else
			echo "PROBE_RC=1"
		fi
		""")
		r = _run_bash(script, cwd=repo_dir, env={"GH_MOCK_PR_LIST_JSON": sibling_list})
		assert r.returncode == 0, f"bash itself failed: {r.stderr}"
		assert "PROBE_RC=1" in r.stdout, f"expected conflict detection, got stdout={r.stdout!r} stderr={r.stderr!r}"
		assert "[merge-probe] PR #700 conflicts with sibling PR #701" in r.stdout
		# README.md is the conflicting path
		assert "README.md" in r.stdout


def test_probe_clean_merge_returns_zero():
	"""When the sibling only edits an unrelated file, the probe passes."""
	import tempfile
	with tempfile.TemporaryDirectory(prefix="merge-probe-") as td:
		repo_dir = Path(td)
		_init_git_repo(repo_dir)
		_extract_probe_functions(repo_dir)

		sibling_list = json.dumps([
			{"number": 702, "headRefName": "sibling-clean", "headRefOid": "cafebabe"}
		])
		script = _mock_gh_script() + textwrap.dedent(r"""
		source extracted/probe.sh
		if probe_sibling_merge_conflicts "700" "candidate" "integration"; then
			echo "PROBE_RC=0"
		else
			echo "PROBE_RC=1"
		fi
		""")
		r = _run_bash(script, cwd=repo_dir, env={"GH_MOCK_PR_LIST_JSON": sibling_list})
		assert r.returncode == 0, f"bash failed: {r.stderr}"
		assert "PROBE_RC=0" in r.stdout, f"expected clean merge, got {r.stdout!r}"


def test_probe_skips_self_from_sibling_list():
	"""A PR is not compared against itself. If the only open PR in the
	list is the candidate, the probe returns 0."""
	import tempfile
	with tempfile.TemporaryDirectory(prefix="merge-probe-") as td:
		repo_dir = Path(td)
		_init_git_repo(repo_dir)
		_extract_probe_functions(repo_dir)

		sibling_list = json.dumps([
			{"number": 700, "headRefName": "candidate", "headRefOid": "cafebabe"}
		])
		script = _mock_gh_script() + textwrap.dedent(r"""
		source extracted/probe.sh
		if probe_sibling_merge_conflicts "700" "candidate" "integration"; then
			echo "PROBE_RC=0"
		else
			echo "PROBE_RC=1"
		fi
		""")
		r = _run_bash(script, cwd=repo_dir, env={"GH_MOCK_PR_LIST_JSON": sibling_list})
		assert r.returncode == 0, f"bash failed: {r.stderr}"
		assert "PROBE_RC=0" in r.stdout, f"expected no conflict against self, got {r.stdout!r}"


def test_probe_empty_sibling_list_returns_zero():
	"""When there are no other open PRs, the probe is a no-op."""
	import tempfile
	with tempfile.TemporaryDirectory(prefix="merge-probe-") as td:
		repo_dir = Path(td)
		_init_git_repo(repo_dir)
		_extract_probe_functions(repo_dir)

		script = _mock_gh_script() + textwrap.dedent(r"""
		source extracted/probe.sh
		if probe_sibling_merge_conflicts "700" "candidate" "integration"; then
			echo "PROBE_RC=0"
		else
			echo "PROBE_RC=1"
		fi
		""")
		r = _run_bash(script, cwd=repo_dir, env={"GH_MOCK_PR_LIST_JSON": "[]"})
		assert r.returncode == 0, f"bash failed: {r.stderr}"
		assert "PROBE_RC=0" in r.stdout


def test_probe_missing_inputs_returns_zero():
	"""Caller-side fail-open: if integration branch or head ref is
	empty, the probe returns 0 so the normal merge attempt proceeds."""
	import tempfile
	with tempfile.TemporaryDirectory(prefix="merge-probe-") as td:
		repo_dir = Path(td)
		_init_git_repo(repo_dir)
		_extract_probe_functions(repo_dir)

		script = _mock_gh_script() + textwrap.dedent(r"""
		source extracted/probe.sh
		if probe_sibling_merge_conflicts "" "" ""; then
			echo "PROBE_RC=0"
		else
			echo "PROBE_RC=1"
		fi
		""")
		r = _run_bash(script, cwd=repo_dir, env={"GH_MOCK_PR_LIST_JSON": "[]"})
		assert r.returncode == 0, f"bash failed: {r.stderr}"
		assert "PROBE_RC=0" in r.stdout


def test_bump_merge_deferral_count_persists_to_state():
	"""_bump_merge_deferral_count increments the counter on the wave
	entry matching the github_issue."""
	import tempfile
	with tempfile.TemporaryDirectory(prefix="merge-probe-") as td:
		repo_dir = Path(td)
		_init_git_repo(repo_dir)
		_extract_probe_functions(repo_dir)

		state = {
			"waves": [
				{"issues": [
					{"id": "a", "github_issue": 10, "status": "pending"},
					{"id": "b", "github_issue": 11, "status": "pending"},
				]},
			]
		}
		state_file = repo_dir / "state.json"
		state_file.write_text(json.dumps(state), encoding="utf-8")

		script = _mock_gh_script() + textwrap.dedent(f"""
		STATE_FILE="{state_file}"
		source extracted/probe.sh
		out="$(_bump_merge_deferral_count 0 10)"
		echo "FIRST=${{out}}"
		out="$(_bump_merge_deferral_count 0 10)"
		echo "SECOND=${{out}}"
		out="$(_bump_merge_deferral_count 0 11)"
		echo "OTHER=${{out}}"
		""")
		r = _run_bash(script, cwd=repo_dir)
		assert r.returncode == 0, f"bash failed: {r.stderr}"
		assert "FIRST=1" in r.stdout, f"first bump should yield 1, got {r.stdout!r}"
		assert "SECOND=2" in r.stdout, f"second bump should yield 2, got {r.stdout!r}"
		assert "OTHER=1" in r.stdout, f"other issue bump should be independent, got {r.stdout!r}"
		final_state = json.loads(state_file.read_text(encoding="utf-8"))
		counters = {i["github_issue"]: i.get("merge_deferral_count") for i in final_state["waves"][0]["issues"]}
		assert counters == {10: 2, 11: 1}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


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
		except AssertionError as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
		except Exception as e:
			print(f"  ERROR {name}: {type(e).__name__}: {e}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
