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
echo 'declare -gA _MERGE_PROBE_CACHE_STATE=()' >> extracted/probe.sh
echo '_MERGE_PROBE_FAIL_CLOSED_SWITCH_WARNED=""' >> extracted/probe.sh
extract_fn '_merge_probe_sibling_lookup_fail_closed_enabled' >> extracted/probe.sh
echo '' >> extracted/probe.sh
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
	caller-controlled JSON array (from $GH_MOCK_PR_LIST_JSON).

	GH_MOCK_PR_LIST_RC (non-zero) makes `gh pr list` print nothing and
	fail; GH_MOCK_CALL_LOG names a file that gets one line per call."""
	return textwrap.dedent(r"""
	gh() {
		if [ "${1:-}" = "pr" ] && [ "${2:-}" = "list" ]; then
			if [ -n "${GH_MOCK_CALL_LOG:-}" ]; then
				echo "pr list" >> "${GH_MOCK_CALL_LOG}"
			fi
			if [ "${GH_MOCK_PR_LIST_RC:-0}" != "0" ]; then
				return "${GH_MOCK_PR_LIST_RC}"
			fi
			printf '%s' "${GH_MOCK_PR_LIST_JSON-[]}"
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


def _run_probe_twice(env: dict[str, str]) -> tuple[subprocess.CompletedProcess, int]:
	"""Run the probe twice in one shell; return the result and gh call count."""
	import tempfile
	with tempfile.TemporaryDirectory(prefix="merge-probe-") as td:
		return _run_probe_twice_in(Path(td), env)


def _run_probe_twice_in(repo_dir: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess, int]:
	_init_git_repo(repo_dir)
	_extract_probe_functions(repo_dir)
	call_log = repo_dir / "gh_calls.log"
	script = _mock_gh_script() + textwrap.dedent(r"""
	source extracted/probe.sh
	rc=0
	probe_sibling_merge_conflicts "700" "candidate" "integration" || rc=$?
	echo "PROBE_RC1=${rc}"
	rc=0
	probe_sibling_merge_conflicts "700" "candidate" "integration" || rc=$?
	echo "PROBE_RC2=${rc}"
	echo "STATE=${_MERGE_PROBE_CACHE_STATE[integration]:-none}"
	""")
	full_env = {"GH_MOCK_CALL_LOG": str(call_log)}
	full_env.update(env)
	r = _run_bash(script, cwd=repo_dir, env=full_env)
	return r, _call_count(call_log)


def _call_count(call_log: Path) -> int:
	if not call_log.exists():
		return 0
	return len([ln for ln in call_log.read_text(encoding="utf-8").splitlines() if ln.strip()])


def test_probe_failed_lookup_returns_two_and_caches_unknown():
	r, calls = _run_probe_twice({"GH_MOCK_PR_LIST_RC": "1"})
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	assert "PROBE_RC1=2" in r.stdout, r.stdout
	assert "PROBE_RC2=2" in r.stdout, r.stdout
	assert "STATE=unknown" in r.stdout, r.stdout
	assert (
		"  [merge-probe] Sibling PR lookup for integration unavailable; cannot probe PR #700 this tick."
		in r.stdout
	), r.stdout
	assert calls == 1, f"expected one gh call, got {calls}"


def test_probe_non_array_lookup_returns_two():
	r, _ = _run_probe_twice({"GH_MOCK_PR_LIST_JSON": '{"message":"x"}'})
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	assert "PROBE_RC1=2" in r.stdout, r.stdout
	assert "STATE=unknown" in r.stdout, r.stdout


def test_probe_empty_output_lookup_returns_two():
	r, _ = _run_probe_twice({"GH_MOCK_PR_LIST_JSON": ""})
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	assert "PROBE_RC1=2" in r.stdout, r.stdout
	assert "STATE=unknown" in r.stdout, r.stdout


def test_probe_valid_empty_array_is_known_and_returns_zero():
	r, calls = _run_probe_twice({"GH_MOCK_PR_LIST_JSON": "[]"})
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	assert "PROBE_RC1=0" in r.stdout, r.stdout
	assert "PROBE_RC2=0" in r.stdout, r.stdout
	assert "STATE=known" in r.stdout, r.stdout
	assert calls == 1


def test_probe_switch_false_restores_fail_open():
	for value in ("false", "FALSE", "False"):
		r, _ = _run_probe_twice({
			"GH_MOCK_PR_LIST_RC": "1",
			"MERGE_PROBE_SIBLING_LOOKUP_FAIL_CLOSED_ENABLED": value,
		})
		assert r.returncode == 0, f"bash failed: {r.stderr}"
		assert "PROBE_RC1=0" in r.stdout, (value, r.stdout)
		assert "::warning::" not in r.stderr, (value, r.stderr)


def test_probe_switch_invalid_value_warns_once_without_value():
	r, _ = _run_probe_twice({
		"GH_MOCK_PR_LIST_RC": "1",
		"MERGE_PROBE_SIBLING_LOOKUP_FAIL_CLOSED_ENABLED": "garbage",
	})
	assert r.returncode == 0, f"bash failed: {r.stderr}"
	assert "PROBE_RC1=0" in r.stdout, r.stdout
	assert r.stderr.count("::warning::MERGE_PROBE_SIBLING_LOOKUP_FAIL_CLOSED_ENABLED") == 1, r.stderr
	assert "garbage" not in r.stdout + r.stderr


def test_probe_switch_true_or_empty_stays_fail_closed():
	for value in ("TRUE", "true", ""):
		r, _ = _run_probe_twice({
			"GH_MOCK_PR_LIST_RC": "1",
			"MERGE_PROBE_SIBLING_LOOKUP_FAIL_CLOSED_ENABLED": value,
		})
		assert r.returncode == 0, f"bash failed: {r.stderr}"
		assert "PROBE_RC1=2" in r.stdout, (value, r.stdout)


def test_ready_to_merge_caller_skips_unknown_lookup_without_counting():
	text = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "if ! probe_sibling_merge_conflicts" not in text
	start = text.index('probe_sibling_merge_conflicts "${RTM_PR}"')
	end = text.index("_bump_merge_deferral_count", start)
	region = text[start:end]
	assert "|| _rtm_probe_rc=$?" in region
	rc2_start = region.index('if [ "${_rtm_probe_rc}" -eq 2 ]; then')
	rc2_end = region.index("\n            fi\n", rc2_start)
	rc2_block = region[rc2_start:rc2_end]
	assert (
		"  [merge-probe] Deferring merge of PR #${RTM_PR} for issue #${rtm_issue} this tick: "
		"sibling lookup unavailable (not counted toward MAX_MERGE_DEFERRALS)."
	) in rc2_block
	assert "continue" in rc2_block
	for forbidden in (
		"_bump_merge_deferral_count",
		"_merge_deferral_cap_alert",
		"unblock_handover_merge_deferral",
		"gh pr merge",
		"STATE_FILE",
		"post_state_comment",
	):
		assert forbidden not in rc2_block, forbidden
	assert 'if [ "${_rtm_probe_rc}" -ne 0 ]; then' in region[rc2_end:]


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
