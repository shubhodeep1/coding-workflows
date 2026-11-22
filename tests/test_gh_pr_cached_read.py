#!/usr/bin/env python3
"""Tests for the run-local PR read cache in scripts/gh_helpers.sh (issue #9928).

``gh_pr_cached_read <kind> <repo> <number> [<head_sha>]`` fetches each PR
resource once per job and head SHA into ``${RUNNER_TEMP}/gh_pr_read_cache``;
``gh_retry``, ``gh_retry_to_file`` and ``gh_api_retry`` evict a PR's entries
after a write to it. These tests drive the helpers through a fake ``gh`` on
PATH that logs every call, and check the static contracts the review job
relies on: the merge path keeps one live head read, the shared
``pr_checks_lib.sh`` sites are gated to the review job, and the workflow wires
the kill switch, the eviction after a push and the summary log line.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
PR_CHECKS_LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
ENABLE_AUTO_MERGE = REPO_ROOT / "scripts" / "review_enable_auto_merge.sh"
COLLECT_PR_METADATA = REPO_ROOT / "scripts" / "review_collect_pr_metadata.sh"
RUN_REVIEWERS = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"

SHA_A = "a" * 40
SHA_B = "b" * 40

FAKE_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${CALL_LOG}"
if [ -e "${FAKE_DIR}/fail" ]; then
	echo "gh: Not Found (HTTP 404)" >&2
	exit 1
fi
if [ -n "${FAKE_SLEEP:-}" ]; then
	sleep "${FAKE_SLEEP}"
fi
if [ "${1:-}" != "api" ]; then
	exit 0
fi
case " $* " in
	*" -X "*|*" --method "*) printf '{}'; exit 0 ;;
esac
case "$*" in
	*"/pulls/"*"/files"*) printf '[{"filename":"a.py"}]' ;;
	*"/issues/"*"/comments"*) printf '[{"id":1}]\n[{"id":2}]\n' ;;
	*"/pulls/"*"/reviews"*) printf '[]' ;;
	*"/pulls/"*"/comments"*) printf '[{"id":3}]' ;;
	*"/check-runs"*) printf '{"total_count":2,"check_runs":[{"id":1}]}{"total_count":2,"check_runs":[{"id":2}]}' ;;
	*"/pulls/"*) cat "${FAKE_DIR}/pull.json" ;;
	*) printf '{}' ;;
esac
"""


def _setup(tmp_path: Path, state: str = "open", head: str = SHA_A) -> dict[str, str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH)
	gh.chmod(0o755)
	fake_dir = tmp_path / "fake"
	fake_dir.mkdir(exist_ok=True)
	(fake_dir / "pull.json").write_text(json.dumps({"number": 7, "state": state, "head": {"sha": head}, "labels": []}))
	runner = tmp_path / "runner"
	runner.mkdir(exist_ok=True)
	env = {
		k: v for k, v in os.environ.items()
		if not k.startswith(("AI_COMMENT_", "GITHUB_", "RUNNER_", "GH_PR_READ_CACHE", "REVIEW_RUN_READ_CACHE", "GH_RETRY_"))
	}
	env.update({
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"RUNNER_TEMP": str(runner),
		"GITHUB_RUN_ID": "901",
		"GITHUB_RUN_ATTEMPT": "1",
		"GITHUB_JOB": "codex-agent",
		"CALL_LOG": str(tmp_path / "calls"),
		"FAKE_DIR": str(fake_dir),
		"GH_TOKEN": "fake",
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	return env


def _run(env: dict[str, str], script: str) -> subprocess.CompletedProcess[str]:
	full = f'set -euo pipefail\nsource "{GH_HELPERS}"\n{script}\n'
	return subprocess.run(["bash", "-c", full], env=env, capture_output=True, text=True, timeout=120)


def _calls(env: dict[str, str], needle: str = "") -> int:
	path = Path(env["CALL_LOG"])
	if not path.exists():
		return 0
	return sum(1 for line in path.read_text().splitlines() if needle in line)


def _cache_dir(env: dict[str, str]) -> Path:
	return Path(env["RUNNER_TEMP"]) / "gh_pr_read_cache"


def test_same_kind_pr_and_head_make_one_call_new_head_makes_another(tmp_path: Path) -> None:
	env = _setup(tmp_path)
	res = _run(env, f"gh_pr_cached_read files o/r 7 {SHA_A}; gh_pr_cached_read files o/r 7 {SHA_A}")
	assert res.returncode == 0, res.stderr
	lines = res.stdout.splitlines()
	assert json.loads(lines[0]) == json.loads(lines[1]) == [{"filename": "a.py"}]
	assert _calls(env, "/files") == 1
	res = _run(env, f"gh_pr_cached_read files o/r 7 {SHA_B}")
	assert res.returncode == 0, res.stderr
	assert _calls(env, "/files") == 2
	stats = (_cache_dir(env) / "901_1.stats").read_text().split("\n")
	assert stats.count("miss files") == 2 and stats.count("hit files") == 1
	assert oct(_cache_dir(env).stat().st_mode & 0o777) == "0o700"


def test_pull_entry_past_ttl_is_refetched(tmp_path: Path) -> None:
	env = _setup(tmp_path)
	res = _run(env, "gh_pr_cached_read pull o/r 7 >/dev/null; gh_pr_cached_read pull o/r 7")
	assert res.returncode == 0, res.stderr
	assert json.loads(res.stdout)["state"] == "open"
	assert _calls(env, "pulls/7") == 1
	meta = next(_cache_dir(env).glob("*_pull_na.json.meta"))
	fields = meta.read_text().split()
	assert fields[:4] == ["o/r", "7", "pull", "na"]
	meta.write_text(" ".join(fields[:4] + ["0"]) + "\n")
	res = _run(env, "gh_pr_cached_read pull o/r 7")
	assert res.returncode == 0, res.stderr
	assert _calls(env, "pulls/7") == 2


def test_closed_pull_is_not_cached(tmp_path: Path) -> None:
	env = _setup(tmp_path, state="closed")
	res = _run(env, "gh_pr_cached_read pull o/r 7 >/dev/null; gh_pr_cached_read pull o/r 7")
	assert res.returncode == 0, res.stderr
	assert json.loads(res.stdout)["state"] == "closed"
	assert _calls(env, "pulls/7") == 2
	assert not list(_cache_dir(env).glob("*_pull_*.json"))


def test_pull_head_mismatch_is_a_miss(tmp_path: Path) -> None:
	env = _setup(tmp_path, head=SHA_A)
	res = _run(env, f"gh_pr_cached_read pull o/r 7 {SHA_B} >/dev/null; gh_pr_cached_read pull o/r 7 {SHA_B}")
	assert res.returncode == 0, res.stderr
	assert json.loads(res.stdout)["head"]["sha"] == SHA_A
	assert _calls(env, "pulls/7") == 2


def test_write_through_each_wrapper_evicts_only_that_pr(tmp_path: Path) -> None:
	env = _setup(tmp_path)
	prime = f"gh_pr_cached_read issue_comments o/r 7 {SHA_A} >/dev/null; gh_pr_cached_read issue_comments o/r 8 {SHA_A} >/dev/null"
	writes = (
		"gh_retry gh api -X POST repos/o/r/issues/7/labels -f 'labels[]=x'",
		"gh_retry gh api repos/o/r/issues/7/comments -f body=hello",
		"gh_retry_to_file \"$RUNNER_TEMP/out\" gh api -X DELETE repos/o/r/issues/7/labels/x",
		"gh_api_retry -X PUT repos/o/r/pulls/7/update-branch",
		"gh_retry _safe_gh_jq -X PATCH repos/o/r/pulls/7 -f body=x",
		"gh_retry gh issue edit 7 --repo o/r --add-label x",
		"gh_retry gh pr comment 7 --body hi",
	)
	for i, write in enumerate(writes):
		res = _run(env, f"{prime}\n{write} >/dev/null")
		assert res.returncode == 0, (write, res.stderr)
		before7 = _calls(env, "issues/7/comments?")
		before8 = _calls(env, "issues/8/comments?")
		res = _run(env, f"gh_pr_cached_read issue_comments o/r 7 {SHA_A} >/dev/null; gh_pr_cached_read issue_comments o/r 8 {SHA_A} >/dev/null")
		assert res.returncode == 0, res.stderr
		assert _calls(env, "issues/7/comments?") == before7 + 1, write
		assert _calls(env, "issues/8/comments?") == before8, write


def test_get_does_not_evict_and_write_stdout_is_unchanged(tmp_path: Path) -> None:
	env = _setup(tmp_path)
	res = _run(env, f"gh_pr_cached_read files o/r 7 {SHA_A} >/dev/null; gh_retry gh api repos/o/r/issues/7/labels >/dev/null; gh_pr_cached_read files o/r 7 {SHA_A} >/dev/null")
	assert res.returncode == 0, res.stderr
	assert _calls(env, "/files") == 1
	res = _run(env, "gh_retry gh api -X POST repos/o/r/issues/7/labels -f 'labels[]=x'")
	assert res.returncode == 0 and res.stdout == "{}", res.stderr


def test_evict_with_kinds_keeps_other_kinds(tmp_path: Path) -> None:
	env = _setup(tmp_path)
	prime = f"gh_pr_cached_read issue_comments o/r 7 {SHA_A} >/dev/null; gh_pr_cached_read files o/r 7 {SHA_A} >/dev/null"
	res = _run(env, f"{prime}\ngh_pr_cache_evict o/r 7 issue_comments\n{prime}")
	assert res.returncode == 0, res.stderr
	assert _calls(env, "issues/7/comments?") == 2
	assert _calls(env, "/files") == 1


def test_cache_unavailable_falls_back_to_live_reads(tmp_path: Path) -> None:
	env = _setup(tmp_path)
	not_a_dir = tmp_path / "runner_file"
	not_a_dir.write_text("x")
	cases = (
		{"RUNNER_TEMP": str(not_a_dir)},
		{"REVIEW_RUN_READ_CACHE_ENABLED": "FALSE"},
		{"REVIEW_RUN_READ_CACHE_ENABLED": "off"},
	)
	expected = 0
	for extra in cases:
		res = _run({**env, **extra}, f"gh_pr_cached_read files o/r 7 {SHA_A}; gh_pr_cached_read files o/r 7 {SHA_A}")
		assert res.returncode == 0, (extra, res.stderr)
		assert [json.loads(x) for x in res.stdout.splitlines()] == [[{"filename": "a.py"}]] * 2
		assert res.stderr == "", res.stderr
		expected += 2
		assert _calls(env, "/files") == expected
	assert not _cache_dir(env).exists()
	no_runner = {k: v for k, v in env.items() if k != "RUNNER_TEMP"}
	res = _run(no_runner, "gh_pr_cached_read pull o/r 7")
	assert res.returncode == 0 and json.loads(res.stdout)["number"] == 7, res.stderr


def test_failed_live_fetch_returns_1_with_no_stdout_and_caches_nothing(tmp_path: Path) -> None:
	env = _setup(tmp_path)
	(Path(env["FAKE_DIR"]) / "fail").write_text("")
	res = _run(env, f"gh_pr_cached_read files o/r 7 {SHA_A} || echo rc=$?")
	assert res.stdout == "rc=1\n", res.stderr
	assert not list(_cache_dir(env).glob("*.json"))


def test_cache_write_failure_returns_live_payload_silently(tmp_path: Path) -> None:
	# Cache setup succeeds; only the store step fails: the temp file, the
	# entry move, or the metadata move. Each read still prints the live
	# payload with rc 0 and nothing on stderr, and nothing usable is stored.
	stubs = (
		'mktemp() { case "${1:-}" in *.json.tmp.*) return 1 ;; esac; command mktemp "$@"; }',
		'mv() { case "${!#}" in *.json) return 1 ;; esac; command mv "$@"; }',
		'mv() { case "${!#}" in *.json.meta) return 1 ;; esac; command mv "$@"; }',
	)
	for i, stub in enumerate(stubs):
		case_dir = tmp_path / str(i)
		case_dir.mkdir()
		env = _setup(case_dir)
		res = _run(env, f"{stub}\ngh_pr_cached_read files o/r 7 {SHA_A}; gh_pr_cached_read files o/r 7 {SHA_A}")
		assert res.returncode == 0, (stub, res.stderr)
		assert res.stderr == "", (stub, res.stderr)
		assert [json.loads(x) for x in res.stdout.splitlines()] == [[{"filename": "a.py"}]] * 2, stub
		assert _calls(env, "/files") == 2, stub
		assert not list(_cache_dir(env).glob("*.json.meta")), stub
		assert not list(_cache_dir(env).glob("*.tmp.*")), stub


def test_paginated_kinds_store_one_combined_value(tmp_path: Path) -> None:
	env = _setup(tmp_path)
	res = _run(env, f"gh_pr_cached_read issue_comments o/r 7 {SHA_A}; gh_pr_cached_read check_runs o/r 7 {SHA_A}")
	assert res.returncode == 0, res.stderr
	comments, checks = (json.loads(x) for x in res.stdout.splitlines())
	assert comments == [{"id": 1}, {"id": 2}]
	assert checks == {"total_count": 2, "check_runs": [{"id": 1}, {"id": 2}]}
	entry = next(_cache_dir(env).glob(f"*_issue_comments_{SHA_A}.json"))
	assert json.loads(entry.read_text()) == [{"id": 1}, {"id": 2}]


def test_invalid_input_makes_no_call(tmp_path: Path) -> None:
	env = _setup(tmp_path)
	for args in ("bogus o/r 7", "pull ../.. 7", "pull o/r/x 7", "pull o/r 0", f"pull o/r 7x {SHA_A}", "check_runs o/r 7", "check_runs o/r 7 abc"):
		res = _run(env, f"gh_pr_cached_read {args} || echo rc=$?")
		assert res.stdout == "rc=1\n", (args, res.stdout, res.stderr)
	assert _calls(env) == 0


def test_concurrent_readers_share_one_fetch(tmp_path: Path) -> None:
	env = {**_setup(tmp_path), "FAKE_SLEEP": "0.5"}
	out = tmp_path / "out"
	out.mkdir()
	script = "\n".join(f'gh_pr_cached_read files o/r 7 {SHA_A} > "{out}/{i}" &' for i in range(8)) + "\nwait"
	res = _run(env, script)
	assert res.returncode == 0, res.stderr
	assert _calls(env, "/files") == 1
	for i in range(8):
		assert json.loads((out / str(i)).read_text()) == [{"filename": "a.py"}]


def _function_body(text: str, name: str) -> str:
	match = re.search(rf"^{re.escape(name)}\(\)\n\{{\n(.*?)^\}}\n", text, re.M | re.S)
	assert match, name
	return match.group(1)


def test_every_retry_wrapper_evicts_after_its_command() -> None:
	text = GH_HELPERS.read_text()
	for name in ("gh_retry", "gh_retry_to_file", "gh_api_retry"):
		assert "_gh_pr_cache_evict_for_cmd" in _function_body(text, name), name


def test_bare_marker_post_in_pr_checks_lib_evicts_comments() -> None:
	# _pr_base_sync_marker_post posts once through bare _safe_gh_jq (no retry
	# wrapper hook), so it must evict the cached issue comments itself.
	body = _function_body(PR_CHECKS_LIB.read_text(), "_pr_base_sync_marker_post")
	post = body.index('_safe_gh_jq -X POST "repos/${repo}/issues/${pr_number}/comments"')
	evict = body.index('gh_pr_cache_evict "${repo}" "${pr_number}" issue_comments')
	assert post < evict


def test_auto_merge_keeps_one_live_head_read_before_merge() -> None:
	lines = ENABLE_AUTO_MERGE.read_text().splitlines()
	assert not any("gh_pr_cached_read" in line for line in lines)
	reads = [i for i, line in enumerate(lines) if 'pulls/${PR_NUMBER}"' in line and "gh api" in line]
	merges = [i for i, line in enumerate(lines) if "gh pr merge" in line and "--auto" in line and "gh_retry" in line]
	assert merges and all("--match-head-commit" in lines[i] for i in merges)
	top_level = [i for i in reads if lines[i].startswith("if ")]
	assert len(top_level) == 1
	assert all(top_level[0] < m for m in merges)
	func_start = next(i for i, line in enumerate(lines) if line.startswith("reviewed_head_is_current_for_labels()"))
	func_end = next(i for i in range(func_start + 1, len(lines)) if lines[i] == "}")
	assert all(i == top_level[0] or func_start < i < func_end for i in reads)


def test_pr_checks_lib_sites_are_gated_to_the_review_job() -> None:
	text = PR_CHECKS_LIB.read_text()
	for kind in ("files", "issue_comments"):
		pattern = (
			r'if \[ -n "\$\{GH_PR_READ_CACHE_JOB:-\}" \] && type gh_pr_cached_read >/dev/null 2>&1; then\n'
			rf'\t\t[a-z_]+="\$\(gh_pr_cached_read {kind} '
		)
		assert re.search(pattern, text), kind


def test_review_scripts_read_through_the_cache() -> None:
	collect = COLLECT_PR_METADATA.read_text()
	for kind in ("pull", "issue_comments", "reviews", "review_comments"):
		assert f"gh_pr_cached_read {kind} " in collect, kind
	run_reviewers = RUN_REVIEWERS.read_text()
	assert "gh_pr_cached_read pull " in run_reviewers
	# PR_PAYLOAD_FILE may be written from a cached entry, so with the cache on
	# the pre-flight must not trust its mtime as the payload age.
	assert 'if [ "${preflight_use_cache}" != "true" ] && [ -s "${PR_PAYLOAD_FILE:-/dev/null}" ]; then' in run_reviewers
	assert "_gh_pr_cache_enabled; then\n    preflight_use_cache=true" in run_reviewers


def test_workflow_wires_cache_env_eviction_and_summary(tmp_path: Path) -> None:
	text = WORKFLOW.read_text()
	assert "      REVIEW_RUN_READ_CACHE_ENABLED: ${{ vars.REVIEW_RUN_READ_CACHE_ENABLED || 'true' }}\n" in text
	assert "      GH_PR_READ_CACHE_JOB: codex-agent\n" in text
	check_step = text.split("- name: Check PR state (defense-in-depth)", 1)[1].split("- name:", 1)[0]
	assert 'pr_meta="$(gh_pr_cached_read pull "${REPOSITORY}" "${PR_NUMBER}" || echo \'null\')"' in check_step
	push_step = text.split("- name: Push all pending commits", 1)[1].split("\n      - name:", 1)[0]
	assert 'gh_pr_cache_evict "${GITHUB_REPOSITORY}" "${PR_NUMBER:-}"' in push_step
	end_step = text.split("- name: Record GH_PAT budget at codex-agent end", 1)[1].split("\n      - name:", 1)[0]
	summary = next(line.strip() for line in end_step.splitlines() if "GH_PR_READ_CACHE hits=" in line)
	runner = tmp_path / "runner"
	cache = runner / "gh_pr_read_cache"
	env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(runner), "GITHUB_RUN_ID": "5", "GITHUB_RUN_ATTEMPT": "2"}
	res = subprocess.run(["bash", "-c", summary], env=env, capture_output=True, text=True, timeout=30)
	assert res.returncode == 0 and res.stdout == "GH_PR_READ_CACHE hits=0 misses=0 job=codex-agent\n", res.stderr
	cache.mkdir(parents=True)
	(cache / "5_2.stats").write_text("miss pull\nhit pull\nhit files\nmiss files\nmiss files\n")
	res = subprocess.run(["bash", "-c", summary], env=env, capture_output=True, text=True, timeout=30)
	assert res.returncode == 0 and res.stdout == "GH_PR_READ_CACHE hits=2 misses=3 job=codex-agent\n", res.stderr
