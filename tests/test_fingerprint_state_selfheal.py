#!/usr/bin/env python3
"""Runtime tests for `_purge_stale_fingerprint_entries_on_integration_branch`.

The orchestrator's wave-dispatch fingerprint gate self-heals stale
`merged_issue_fingerprints` entries that capture is idempotent and so
cannot self-correct. Two predicates apply:

- **Tier 1 (no reference):** the recorded PR has no commit matching
  `(#<pr>)` anywhere on the integration branch. The captured diff
  cannot be on the branch. This catches the pre-PR-#2907 capture bug
  where capture latched onto an open `Refs #N` cross-reference PR that
  was never merged anywhere.

- **Tier 2 (capture predates merge):** the recorded PR DOES have a
  merge commit on the branch (subject ending `(#<pr>)`), but
  `captured_at` < that commit's committer date. Capture ran from a
  pre-merge open-PR snapshot whose content was iterated before the
  squash-merge landed. Reproduces project #2867 / issue #2872 exactly
  (capture 2026-05-22T09:04:12Z; PR #2894 merge 2026-05-22T10:59:08Z;
  the REST-fallback half rewritten in between).

Healthy entries (capture ran AFTER the merge via the orchestrator's
normal flow) have `captured_at` > merge committer date and must be
kept so a genuine post-merge resolver regression still hard-fails the
gate as designed.

Uses the same function-extraction-plus-stub pattern as
`test_stall_recovery_pr_lookup.py` and `test_subissue_closing_pr.py`,
but with a real on-disk git repo (because the helper shells out to
`git log` with `--grep` and `--format=%ct`). Commits are built via
`git commit-tree` to avoid the sandbox's commit-signing hook
interfering with `git commit`.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
FINGERPRINT_VERIFIER = REPO_ROOT / "scripts" / "verify_integration_fingerprints.py"


_EXTRACT_FN = r"""
extract_fn() {
	local fn="$1"
	awk -v fn="${fn}" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__POLLER__"
}
"""


def _run_bash(script: str, cwd: Path) -> subprocess.CompletedProcess:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["GIT_CONFIG_GLOBAL"] = "/dev/null"  # ignore any host-level sign hooks
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=env,
		capture_output=True,
		text=True,
	)


def _bootstrap(tmp: Path) -> Path:
	"""Extract the helper into helpers.sh and return its path."""
	extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
	r = _run_bash(
		textwrap.dedent(
			f"""
			set -euo pipefail
			{extractor}
			: > helpers.sh
			extract_fn '_purge_stale_fingerprint_entries_on_integration_branch' >> helpers.sh
			"""
		),
		cwd=tmp,
	)
	assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
	helpers = tmp / "helpers.sh"
	assert helpers.exists() and helpers.stat().st_size > 0
	return helpers


def _init_repo(repo: Path) -> str:
	"""Initialise an empty repo and return the shared empty-tree SHA."""
	repo.mkdir(parents=True, exist_ok=True)
	r = _run_bash(
		"git init -q && git config user.email a@b && git config user.name a "
		"&& git hash-object -t tree --stdin < /dev/null",
		cwd=repo,
	)
	assert r.returncode == 0, r.stderr
	return r.stdout.strip()


def _make_commit(
	repo: Path,
	empty_tree: str,
	*,
	subject: str,
	committer_unix: int,
	parent: str | None = None,
) -> str:
	"""Create a commit via `git commit-tree` (bypasses commit hooks) and
	return its SHA. Subjects can carry the `(#<PR>)` squash-merge marker."""
	parent_flag = f"-p {parent}" if parent else ""
	env_prefix = (
		f"GIT_AUTHOR_DATE='@{committer_unix} +0000' "
		f"GIT_COMMITTER_DATE='@{committer_unix} +0000' "
		"GIT_AUTHOR_NAME=a GIT_AUTHOR_EMAIL=a@b "
		"GIT_COMMITTER_NAME=a GIT_COMMITTER_EMAIL=a@b"
	)
	r = _run_bash(
		f"{env_prefix} git commit-tree {empty_tree} {parent_flag} -m {subject!r}",
		cwd=repo,
	)
	assert r.returncode == 0, f"commit-tree failed: {r.stderr}\n{r.stdout}"
	return r.stdout.strip()


def _set_branch(repo: Path, name: str, sha: str) -> None:
	r = _run_bash(f"git update-ref refs/heads/{name} {sha}", cwd=repo)
	assert r.returncode == 0, r.stderr


def _write_state(state_path: Path, entries: dict[str, dict]) -> None:
	state_path.write_text(
		json.dumps({"merged_issue_fingerprints": entries}, separators=(",", ":")),
		encoding="utf-8",
	)


def _purged(stdout: str) -> dict[str, tuple[str, str]]:
	"""Parse helper stdout into {issue: (pr, reason)}."""
	out = {}
	for line in stdout.splitlines():
		if not line.strip():
			continue
		issue, pr, reason = line.split("\t")
		out[issue] = (pr, reason)
	return out


# ISO-8601 UTC string from a unix timestamp the harness controls.
def _iso(unix_ts: int) -> str:
	return datetime.datetime.fromtimestamp(unix_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_resolver_safe_export_omits_unmerged_and_unsafe_fingerprints():
	with tempfile.TemporaryDirectory() as td:
		state_path = Path(td) / "state.json"
		state_path.write_text(json.dumps({
			"schema_version": "orchestrate_state.v1",
			"waves": [{"issues": [
				{"github_issue": 10, "status": "merged"},
				{"github_issue": 11, "status": "in_progress"},
				{"github_issue": 12, "status": "merged"},
				{"github_issue": 13, "status": "merged"},
			]}],
			"merged_issue_fingerprints": {
				"10": {
					"issue": 10,
					"pr": 20,
					"captured_at": "2026-09-04T00:00:00Z",
					"must_contain": [
						{"file": "scripts/app.py", "regex": re.escape("expected literal")},
						{"file": "../outside", "regex": re.escape("unsafe")},
						{"file": ".git/config", "regex": re.escape("unsafe")},
						{"file": "scripts/.git/config", "regex": re.escape("unsafe")},
						{"file": "scripts/control\ninjected.py", "regex": re.escape("unsafe")},
						{"file": "scripts/control\tinjected.py", "regex": re.escape("unsafe")},
						{"file": "scripts/control\x01injected.py", "regex": re.escape("unsafe")},
						{"file": "scripts/raw.py", "regex": ".*"},
						{"file": "scripts/" + ("a" * 4097), "regex": re.escape("oversized")},
					],
					"must_not_contain": [{"file": "/tmp/absolute", "regex": re.escape("unsafe")}],
					"must_not_exist": [
						{"file": "src/deleted.py"},
						{"file": ".git/index"},
						{"file": "src/.git/HEAD"},
						{"file": "src/control\rinjected.py"},
						{"file": "src/control\x7finjected.py"},
					],
				},
				"11": {
					"issue": 11, "pr": 21,
					"must_contain": [{"file": "scripts/unmerged.py", "regex": re.escape("no")}],
				},
				"12": {"issue": 999, "pr": 22, "must_contain": []},
				"13": {
					"issue": 13,
					"pr": 23,
					"must_contain": [
						{"file": "scripts/excess.py", "regex": re.escape("bounded")}
					] * 4097,
				},
			},
		}), encoding="utf-8")
		result = subprocess.run(
			["python3", str(FINGERPRINT_VERIFIER), "--export-resolver-safe-fingerprints", str(state_path)],
			capture_output=True,
			text=True,
			env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		)
	assert result.returncode == 0, result.stderr
	exported = json.loads(result.stdout)
	assert list(exported) == ["10"]
	assert exported["10"]["must_contain"] == [
		{"file": "scripts/app.py", "regex": re.escape("expected literal")},
	]
	assert exported["10"]["must_not_contain"] == []
	assert exported["10"]["must_not_exist"] == [{"file": "src/deleted.py"}]


# ---------------------------------------------------------------------------
# Tier 1 — PR not referenced on the branch at all
# ---------------------------------------------------------------------------


def test_purges_entry_when_pr_has_no_commit_on_branch():
	"""A PR whose number does not appear in any commit subject or body on
	the integration branch cannot have contributed its diff — drop."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="AI implementation for issue #2873 (#2878)", committer_unix=1000)
		_set_branch(repo, "integ", c1)
		state = repo / "state.json"
		_write_state(state, {
			"9999": {"issue": 9999, "pr": 9999, "captured_at": _iso(2000), "must_contain": []},
		})
		# Switch the helper's cwd to the repo so `git log <ref>` resolves locally.
		r = _run_bash(
			textwrap.dedent(
				f"""
				set -uo pipefail
				source {tmp}/helpers.sh
				_purge_stale_fingerprint_entries_on_integration_branch state.json integ
				"""
			),
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		drops = _purged(r.stdout)
		assert drops == {"9999": ("9999", "pr_not_referenced_on_integration_branch")}, r.stdout
		post = json.loads(state.read_text(encoding="utf-8"))
		assert post["merged_issue_fingerprints"] == {}, post


# ---------------------------------------------------------------------------
# Tier 2 — capture predates the PR's merge into the branch
# ---------------------------------------------------------------------------


def test_purges_entry_when_capture_predates_pr_merge_commit():
	"""THE project #2867 / issue #2872 reproduction. PR #2894's merge
	commit IS on the branch, but `captured_at` < merge committer date —
	capture latched onto an open-PR snapshot that was rewritten before
	the squash-merge landed. Drop."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="fix(stall-recovery) (#2894)", committer_unix=5000)
		_set_branch(repo, "integ", c1)
		state = repo / "state.json"
		# captured BEFORE the merge → stale
		_write_state(state, {
			"2872": {"issue": 2872, "pr": 2894, "captured_at": _iso(3000), "must_contain": []},
		})
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f"_purge_stale_fingerprint_entries_on_integration_branch state.json integ",
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		drops = _purged(r.stdout)
		assert drops == {"2872": ("2894", "captured_before_pr_merged_into_integration_branch")}, r.stdout
		assert json.loads(state.read_text())["merged_issue_fingerprints"] == {}


def test_keeps_entry_when_capture_followed_pr_merge_commit():
	"""Healthy sub-issue capture: orchestrator detects merge → captures
	fingerprints right after. `captured_at` > merge committer date.
	Must not be dropped — a real post-merge resolver regression must
	still surface via the verifier."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="AI implementation for issue #2873 (#2878)", committer_unix=1000)
		_set_branch(repo, "integ", c1)
		state = repo / "state.json"
		_write_state(state, {
			"2873": {"issue": 2873, "pr": 2878, "captured_at": _iso(1050), "must_contain": []},
		})
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f"_purge_stale_fingerprint_entries_on_integration_branch state.json integ",
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		assert r.stdout.strip() == "", f"healthy entry should not be purged; got: {r.stdout!r}"
		assert "2873" in json.loads(state.read_text())["merged_issue_fingerprints"]


def test_keeps_entry_when_later_duplicate_suffix_exists_after_capture():
	"""Use the oldest subject-ending `(#<pr>)` commit as the merge.
	A later duplicate suffix (for example a replay or cherry-pick)
	must not retroactively make a healthy entry look stale."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="AI implementation for issue #2873 (#2878)", committer_unix=1000)
		c2 = _make_commit(repo, empty, parent=c1, subject="Replay AI implementation for issue #2873 (#2878)", committer_unix=2000)
		_set_branch(repo, "integ", c2)
		state = repo / "state.json"
		_write_state(state, {
			"2873": {"issue": 2873, "pr": 2878, "captured_at": _iso(1500), "must_contain": []},
		})
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f"_purge_stale_fingerprint_entries_on_integration_branch state.json integ",
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		assert r.stdout.strip() == "", f"oldest merge commit should win; got: {r.stdout!r}"
		assert "2873" in json.loads(state.read_text())["merged_issue_fingerprints"]


def test_keeps_entry_when_only_body_mentions_pr_but_no_merge_commit():
	"""A back-merge or sync commit may mention `(#<pr>)` in its body
	without being the PR's merge commit. The helper requires the
	subject to END with `(#<pr>)` to consider it the merge — but if a
	body mention exists at all, predicate (a) (no reference anywhere)
	does not fire. With no merge commit and no captured_at>merge check
	available, the entry is kept."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		# Subject does NOT end with the suffix, but body mentions it.
		c1 = _make_commit(
			repo, empty,
			subject="chore: sync main into orchestrator/project-foo\n\nIncludes commit (#7777) from main.",
			committer_unix=2000,
		)
		_set_branch(repo, "integ", c1)
		state = repo / "state.json"
		_write_state(state, {
			"500": {"issue": 500, "pr": 7777, "captured_at": _iso(1500), "must_contain": []},
		})
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f"_purge_stale_fingerprint_entries_on_integration_branch state.json integ",
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		assert r.stdout.strip() == "", f"body-only mention should not trigger drop; got: {r.stdout!r}"
		assert "500" in json.loads(state.read_text())["merged_issue_fingerprints"]


def test_word_boundary_distinct_pr_numbers_do_not_collide():
	"""A merge of PR #28730 must NOT satisfy the lookup for PR #2873 —
	`(#28730)` is not a literal substring of `(#2873)` (the closing
	paren breaks the prefix), so git's `--grep` returns zero hits."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="other PR (#28730)", committer_unix=1000)
		_set_branch(repo, "integ", c1)
		state = repo / "state.json"
		# Entry for issue 200 says PR is 2873 — distinct from 28730.
		_write_state(state, {
			"200": {"issue": 200, "pr": 2873, "captured_at": _iso(2000), "must_contain": []},
		})
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f"_purge_stale_fingerprint_entries_on_integration_branch state.json integ",
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		drops = _purged(r.stdout)
		assert drops == {"200": ("2873", "pr_not_referenced_on_integration_branch")}, r.stdout


def test_entry_without_pr_field_is_skipped():
	"""Older state entries that lack a `pr` field — pass over them
	silently (cannot decide either predicate without the PR number)."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="some commit", committer_unix=1000)
		_set_branch(repo, "integ", c1)
		state = repo / "state.json"
		_write_state(state, {
			"300": {"issue": 300, "captured_at": _iso(2000), "must_contain": []},
		})
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f"_purge_stale_fingerprint_entries_on_integration_branch state.json integ",
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		assert r.stdout.strip() == ""
		assert "300" in json.loads(state.read_text())["merged_issue_fingerprints"]


def test_entry_without_captured_at_keeps_when_referenced():
	"""An entry without `captured_at` can only be evaluated under
	predicate (a): if the PR is referenced anywhere on the branch we
	cannot apply the temporal check, so keep the entry — fail-safe."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="impl (#4242)", committer_unix=1000)
		_set_branch(repo, "integ", c1)
		state = repo / "state.json"
		_write_state(state, {
			"400": {"issue": 400, "pr": 4242, "must_contain": []},
		})
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f"_purge_stale_fingerprint_entries_on_integration_branch state.json integ",
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		assert r.stdout.strip() == ""
		assert "400" in json.loads(state.read_text())["merged_issue_fingerprints"]


def test_mixed_state_purges_only_stale_entries():
	"""Round-trip the project #2867 shape — five entries on one branch.
	Healthy (capture>merge) entries stay; the one stale (capture<merge)
	entry drops."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="AI implementation for issue #2868 (#2877)", committer_unix=1100)
		c2 = _make_commit(repo, empty, parent=c1, subject="AI implementation for issue #2873 (#2878)", committer_unix=1200)
		c3 = _make_commit(repo, empty, parent=c2, subject="fix(stall-recovery) (#2894)", committer_unix=5000)
		_set_branch(repo, "integ", c3)
		state = repo / "state.json"
		_write_state(state, {
			# Healthy: captured after merge.
			"2868": {"issue": 2868, "pr": 2877, "captured_at": _iso(1110), "must_contain": []},
			"2873": {"issue": 2873, "pr": 2878, "captured_at": _iso(1210), "must_contain": []},
			# Stale: captured BEFORE PR #2894's merge committer date.
			"2872": {"issue": 2872, "pr": 2894, "captured_at": _iso(3000), "must_contain": []},
			# No reference at all.
			"9999": {"issue": 9999, "pr": 9999, "captured_at": _iso(0), "must_contain": []},
		})
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f"_purge_stale_fingerprint_entries_on_integration_branch state.json integ",
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		drops = _purged(r.stdout)
		assert drops == {
			"2872": ("2894", "captured_before_pr_merged_into_integration_branch"),
			"9999": ("9999", "pr_not_referenced_on_integration_branch"),
		}, r.stdout
		kept = sorted(json.loads(state.read_text())["merged_issue_fingerprints"].keys())
		assert kept == ["2868", "2873"], kept


def test_invalid_pr_value_is_skipped():
	"""A non-numeric `pr` value (corrupt state) is skipped — keep entry."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="x", committer_unix=1000)
		_set_branch(repo, "integ", c1)
		state = repo / "state.json"
		_write_state(state, {
			"600": {"issue": 600, "pr": "not-a-number", "captured_at": _iso(2000), "must_contain": []},
		})
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f"_purge_stale_fingerprint_entries_on_integration_branch state.json integ",
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		assert r.stdout.strip() == ""
		assert "600" in json.loads(state.read_text())["merged_issue_fingerprints"]


def test_missing_state_file_no_op():
	"""Missing state file is a no-op — no failure, no output."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="x", committer_unix=1000)
		_set_branch(repo, "integ", c1)
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f"_purge_stale_fingerprint_entries_on_integration_branch nonexistent.json integ",
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		assert r.stdout.strip() == ""


def test_empty_ref_no_op():
	"""Empty ref argument is a no-op — fail-safe."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		repo = tmp / "repo"
		empty = _init_repo(repo)
		c1 = _make_commit(repo, empty, subject="impl (#1234)", committer_unix=1000)
		_set_branch(repo, "integ", c1)
		state = repo / "state.json"
		_write_state(state, {
			"700": {"issue": 700, "pr": 1234, "captured_at": _iso(500), "must_contain": []},
		})
		r = _run_bash(
			f"set -uo pipefail; source {tmp}/helpers.sh; "
			f'_purge_stale_fingerprint_entries_on_integration_branch state.json ""',
			cwd=repo,
		)
		assert r.returncode == 0, r.stderr
		assert r.stdout.strip() == ""
		assert "700" in json.loads(state.read_text())["merged_issue_fingerprints"]


# ---------------------------------------------------------------------------
# Direct-invocation entrypoint — ci.yml runs each test as `python3 <file>`.
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
		except Exception as e:  # noqa: BLE001
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
