"""Contract for scripts/claude_merge_hold_gate.py (issue #5316): the review
workflow refuses to merge a claude/* PR whose head carries a live `hold` claim
or whose twin-first edit left a workflow-templates/.claude/** twin pair out of
parity, and it is wired before every merge enablement a claude/* PR reaches in
.github/workflows/review_autofix.yml. Issue #5565: an allowed claude/* head is
merged synchronously (never enrolled in auto-merge, which would merge later
without the gate), and every review run cancels a stale enrollment."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "claude_merge_hold_gate.py"
AUTO_MERGE_HELPER = ROOT / "scripts" / "review_enable_auto_merge.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "review_autofix.yml"
_spec = importlib.util.spec_from_file_location("claude_merge_hold_gate", GATE)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

REPO = "o/r"
HEAD = "a" * 40
OLD_HEAD = "b" * 40
BASE = "c" * 40
MERGE_BASE = "d" * 40
AUTHOR = "session-owner"


def _pr(ref="claude/implement-plan-x-phase-1", head=HEAD):
	return {"number": 42, "state": "open", "user": {"login": AUTHOR}, "body": "",
		"head": {"ref": ref, "sha": head}, "base": {"ref": "claude/implement-plan-x", "sha": BASE}}


def _claim(comment_id, kind, head=HEAD, login=AUTHOR, association="OWNER", at="2026-09-29T23:39:07Z", by="session_x"):
	return {"id": comment_id, "user": {"login": login}, "author_association": association, "created_at": at,
		"body": f"claim\n\n<!-- ai:claude-fix-claim:v1 head={head} kind={kind} by={by} -->\n"}


def _tree(blobs, truncated=False):
	return {"truncated": truncated, "tree": [{"path": path, "type": "blob", "sha": sha} for path, sha in blobs.items()]}


def _compare(files):
	return {"merge_base_commit": {"sha": MERGE_BASE}, "files": [{"filename": name} for name in files]}


def _fixture(comments=(), files=("scripts/x.py",), head_blobs=None, base_blobs=None, pr=None, fail=()):
	responses = {
		f"repos/{REPO}/pulls/42": pr or _pr(),
		f"repos/{REPO}/issues/42/comments": list(comments),
		f"repos/{REPO}/compare/{BASE}...{HEAD}": _compare(files),
	}
	if head_blobs is not None:
		responses[f"repos/{REPO}/git/trees/{HEAD}"] = head_blobs if isinstance(head_blobs, dict) and "tree" in head_blobs else _tree(head_blobs)
	if base_blobs is not None:
		responses[f"repos/{REPO}/git/trees/{MERGE_BASE}"] = _tree(base_blobs)
	for path in fail:
		responses[path] = None
	return responses


FAKE_GH = textwrap.dedent(
	"""\
	#!/usr/bin/env python3
	import json, os, sys
	args = sys.argv[1:]
	with open(os.environ["FAKE_GH_CALLS"], "a", encoding="utf-8") as fh:
		fh.write(json.dumps(args) + "\\n")
	fixture = json.load(open(os.environ["FAKE_GH_FIXTURE"], encoding="utf-8"))
	if args[:2] == ["pr", "merge"]:
		if "--disable-auto" in args:
			if os.environ.get("FAKE_GH_DISABLE_FAIL"):
				sys.stderr.write("HTTP 502: Bad Gateway (https://api.github.com/graphql)\\nsecond line\\n")
				sys.exit(1)
			sys.exit(0)
		if os.environ.get("FAKE_GH_MERGE_FAIL") and "--auto" not in args:
			sys.stderr.write("X Pull request is not mergeable: the base branch policy prohibits the merge.\\n")
			sys.exit(1)
		sys.exit(0)
	if args[:3] == ["api", "-X", "PUT"] and args[3].endswith("/merge"):
		if os.environ.get("FAKE_GH_MERGE_FAIL"):
			sys.stderr.write("HTTP 405: Pull Request is not mergeable (https://api.github.com/%s)\\n" % args[3])
			sys.exit(1)
		sys.stdout.write(json.dumps({"merged": True, "sha": "e" * 40}))
		sys.exit(0)
	if args[:2] == ["api", "graphql"]:
		sys.exit(0)
	if args[:1] == ["api"] and "--jq" in args and ".merged == true" in args[args.index("--jq") + 1]:
		sys.stdout.write(os.environ.get("FAKE_GH_MERGED_AT", "false " + "a" * 40))
		sys.exit(0)
	if args[:1] == ["api"] and "--jq" in args and args[args.index("--jq") + 1] == ".auto_merge == null":
		sys.stdout.write("true" if os.environ.get("FAKE_GH_AUTO_MERGE_GONE") else "false")
		sys.exit(0)
	if args[:1] == ["api"]:
		path = next((a for a in args[1:] if "/" in a), "")
		if "/labels" in path:
			sys.exit(0)
		base = path.split("?", 1)[0]
		flaky = fixture.get("__flaky__", {})
		if base in flaky:
			count_path = os.environ["FAKE_GH_CALLS"] + ".flaky." + base.replace("/", "_")
			count = int(open(count_path).read()) if os.path.exists(count_path) else 0
			if count < flaky[base]:
				open(count_path, "w").write(str(count + 1))
				sys.stderr.write("HTTP 502: %s\\n" % path)
				sys.exit(1)
		if base not in fixture or fixture[base] is None:
			sys.stderr.write("HTTP 502: %s\\n" % path)
			sys.exit(1)
		payload = fixture[base]
		query = dict(part.split("=", 1) for part in path.partition("?")[2].split("&") if "=" in part)
		if isinstance(payload, list) and "page" in query:
			size = int(query.get("per_page", max(len(payload), 1)))
			start = (int(query["page"]) - 1) * size
			payload = payload[start:start + size]
		sys.stdout.write(json.dumps(payload))
		sys.exit(0)
	sys.exit(0)
	"""
)


def _fake_gh_env(tmp_path, fixture, extra=None):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	(bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	fixture_path = tmp_path / "fixture.json"
	fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
	env = dict(os.environ)
	env.pop("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", None)
	env.update({"PATH": f"{bin_dir}:{env.get('PATH', '')}", "FAKE_GH_FIXTURE": str(fixture_path),
		"FAKE_GH_CALLS": str(tmp_path / "calls.jsonl"), "PYTHONDONTWRITEBYTECODE": "1"})
	env.update(extra or {})
	return env


def _calls(tmp_path):
	calls_path = tmp_path / "calls.jsonl"
	if not calls_path.exists():
		return []
	return [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_gate(tmp_path, fixture, pr_json=None, extra_env=None, head=HEAD):
	env = _fake_gh_env(tmp_path, fixture, extra_env)
	argv = ["python3", str(GATE), "--repo", REPO, "--pr", "42", "--head", head]
	if pr_json is not None:
		pr_path = tmp_path / "pr.json"
		pr_path.write_text(json.dumps(pr_json), encoding="utf-8")
		argv += ["--pr-json", str(pr_path)]
	proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
	return proc.returncode, json.loads(proc.stdout), _calls(tmp_path)


# --- hold claims ------------------------------------------------------------


def test_hold_posted_after_the_run_started_blocks_the_merge(tmp_path):
	# PR #5301: the run started 23:38:50Z, the hold landed 23:39:07Z, and the
	# gate reads the comments again right before merge enablement.
	code, result, _ = _run_gate(tmp_path, _fixture(comments=[_claim(1, "hold", at="2026-09-29T23:39:07Z")]))
	assert code == 1
	assert result["merge"] is False
	assert result["skip_reason"] == "hold_claim"
	assert result["claim"] == "held"


def test_hold_on_an_older_head_does_not_block(tmp_path):
	code, result, _ = _run_gate(tmp_path, _fixture(comments=[_claim(1, "hold", head=OLD_HEAD)]))
	assert code == 0, result
	assert result["merge"] is True
	assert result["claim"] == "none"


def test_a_newer_claim_on_the_same_head_lifts_the_hold(tmp_path):
	code, result, _ = _run_gate(tmp_path, _fixture(comments=[_claim(1, "hold"), _claim(2, "review", at="2026-09-30T00:10:00Z")]))
	assert code == 0, result
	assert result["merge"] is True


@pytest.mark.parametrize("login,association", [("someone-else", "COLLABORATOR"), (AUTHOR, "NONE"), (AUTHOR, "CONTRIBUTOR")])
def test_untrusted_holds_are_ignored(tmp_path, login, association):
	code, result, _ = _run_gate(tmp_path, _fixture(comments=[_claim(1, "hold", login=login, association=association)]))
	assert code == 0, result


def test_hold_by_the_configured_workflow_account_counts(tmp_path):
	fixture = _fixture(comments=[_claim(1, "hold", login="review-bot-pat")])
	code, result, _ = _run_gate(tmp_path, fixture, extra_env={"CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN": "Review-Bot-PAT"})
	assert code == 1
	assert result["skip_reason"] == "hold_claim"


def test_live_non_hold_claim_does_not_block(tmp_path):
	# The convergence path merges while the fixer's review claim is still live.
	code, result, _ = _run_gate(tmp_path, _fixture(comments=[_claim(1, "review")]))
	assert code == 0, result


def test_non_claude_head_is_allowed_without_any_api_call(tmp_path):
	code, result, calls = _run_gate(tmp_path, _fixture(), pr_json=_pr(ref="ai/issue-42"))
	assert code == 0
	assert result["merge"] is True
	assert calls == []


def test_pr_json_saves_the_pull_read(tmp_path):
	code, _, calls = _run_gate(tmp_path, _fixture(), pr_json=_pr())
	assert code == 0
	paths = [call[1] for call in calls]
	assert not any(path.endswith("/pulls/42") for path in paths), paths
	assert any("/issues/42/comments" in path for path in paths)


# --- twin parity ------------------------------------------------------------

TWIN = "workflow-templates/.claude/hooks/new_guard.py"
LIVE = ".claude/hooks/new_guard.py"
CMD_TWIN = "workflow-templates/.claude/commands/verify-activation.md"
CMD_LIVE = ".claude/commands/verify-activation.md"


def test_twin_changed_without_the_claude_copy_blocks(tmp_path):
	base = {TWIN: "t1", LIVE: "t1"}
	head = {TWIN: "t2", LIVE: "t1"}
	code, result, _ = _run_gate(tmp_path, _fixture(files=[TWIN], head_blobs=head, base_blobs=base))
	assert code == 1
	assert result["skip_reason"] == "twin_parity"
	assert result["broken_pairs"] == [TWIN]


def test_new_twin_without_a_claude_counterpart_blocks(tmp_path):
	code, result, _ = _run_gate(tmp_path, _fixture(files=[TWIN], head_blobs={TWIN: "t1"}, base_blobs={}))
	assert code == 1
	assert result["broken_pairs"] == [TWIN]


def test_change_to_an_already_divergent_pair_is_allowed(tmp_path):
	base = {CMD_TWIN: "consumer1", CMD_LIVE: "internal"}
	head = {CMD_TWIN: "consumer2", CMD_LIVE: "internal"}
	code, result, _ = _run_gate(tmp_path, _fixture(files=[CMD_TWIN], head_blobs=head, base_blobs=base))
	assert code == 0, result


def test_twin_and_claude_copy_changed_together_is_allowed(tmp_path):
	base = {TWIN: "t1", LIVE: "t1"}
	head = {TWIN: "t2", LIVE: "t2"}
	code, result, _ = _run_gate(tmp_path, _fixture(files=[TWIN, LIVE], head_blobs=head, base_blobs=base))
	assert code == 0, result


def test_no_twin_change_skips_the_tree_reads(tmp_path):
	code, _, calls = _run_gate(tmp_path, _fixture(files=["scripts/x.py", ".claude/settings.json"]))
	assert code == 0
	assert not [call for call in calls if "/git/trees/" in call[1]], calls


def test_a_full_compare_page_falls_back_to_the_trees(tmp_path):
	files = [f"docs/f{index}.md" for index in range(gate.COMPARE_FILES_MAX)]
	code, result, calls = _run_gate(tmp_path, _fixture(files=files, head_blobs={TWIN: "t2", LIVE: "t1"}, base_blobs={TWIN: "t1", LIVE: "t1"}))
	assert code == 1
	assert result["skip_reason"] == "twin_parity"
	assert len([call for call in calls if "/git/trees/" in call[1]]) == 2


def test_a_compare_without_a_file_list_falls_back_to_the_trees(tmp_path):
	fixture = _fixture(head_blobs={TWIN: "t2", LIVE: "t1"}, base_blobs={TWIN: "t1", LIVE: "t1"})
	fixture[f"repos/{REPO}/compare/{BASE}...{HEAD}"] = {"merge_base_commit": {"sha": MERGE_BASE}}
	code, result, _ = _run_gate(tmp_path, fixture)
	assert code == 1
	assert result["skip_reason"] == "twin_parity"


def test_twin_parity_breaks_is_pure():
	assert gate.twin_parity_breaks({}, {}) == []
	assert gate.twin_parity_breaks({TWIN: "x", LIVE: "x"}, {TWIN: "y", LIVE: "y"}) == []
	assert gate.twin_parity_breaks({TWIN: "x", LIVE: "y"}, {TWIN: "y", LIVE: "y"}) == [TWIN]
	# A deleted twin whose live copy stays out of parity.
	assert gate.twin_parity_breaks({LIVE: "y"}, {TWIN: "y", LIVE: "y"}) == [TWIN]
	# A live-only change is not a twin change (CI's parity tests own that case).
	assert gate.twin_parity_breaks({TWIN: "y", LIVE: "z"}, {TWIN: "y", LIVE: "y"}) == []


# --- fail closed ------------------------------------------------------------


def test_truncated_tree_fails_closed(tmp_path):
	fixture = _fixture(files=[TWIN], head_blobs=_tree({TWIN: "t2"}, truncated=True), base_blobs={TWIN: "t1", LIVE: "t1"})
	code, result, _ = _run_gate(tmp_path, fixture)
	assert code == 2
	assert result["merge"] is False
	assert result["skip_reason"] == "gate_unavailable"


def test_comment_read_error_fails_closed(tmp_path):
	code, result, _ = _run_gate(tmp_path, _fixture(fail=[f"repos/{REPO}/issues/42/comments"]))
	assert code == 2
	assert result["skip_reason"] == "gate_unavailable"
	assert "HTTP 502" in result["reason"]


def test_head_mismatch_fails_closed(tmp_path):
	code, result, _ = _run_gate(tmp_path, _fixture(pr=_pr(head=OLD_HEAD)))
	assert code == 2
	assert result["skip_reason"] == "gate_unavailable"


@pytest.mark.parametrize("argv", [["--repo", "bad", "--pr", "42", "--head", HEAD], ["--repo", REPO, "--pr", "42", "--head", "abc"]])
def test_bad_arguments_fail_closed(tmp_path, argv):
	env = _fake_gh_env(tmp_path, _fixture())
	proc = subprocess.run(["python3", str(GATE), *argv], env=env, capture_output=True, text=True, check=False)
	assert proc.returncode == 2
	assert json.loads(proc.stdout)["skip_reason"] == "gate_unavailable"


# --- comment flood (issue #5566) --------------------------------------------

COMMENTS_PATH = f"repos/{REPO}/issues/42/comments"


def _flood(count, start_id=1, login="flooder", association="NONE", body=None):
	return [{"id": start_id + offset, "user": {"login": login}, "author_association": association,
		"created_at": "2026-09-30T00:00:00Z",
		"body": body if body is not None else f"spam {start_id + offset}"} for offset in range(count)]


def _comment_page_calls(calls):
	return [call for call in calls if any(COMMENTS_PATH in arg for arg in call)]


def _run_gate_in_process(tmp_path, monkeypatch, capsys, fixture):
	"""Run `main` in this process so the retry back-off can be recorded instead of slept."""
	env = _fake_gh_env(tmp_path, fixture)
	for key in ("PATH", "FAKE_GH_FIXTURE", "FAKE_GH_CALLS"):
		monkeypatch.setenv(key, env[key])
	monkeypatch.delenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", raising=False)
	sleeps = []
	monkeypatch.setattr(gate.time, "sleep", sleeps.append)
	code = gate.main(["--repo", REPO, "--pr", "42", "--head", HEAD])
	return code, json.loads(capsys.readouterr().out), _calls(tmp_path), sleeps


def test_hold_on_page_eleven_of_a_comment_flood_blocks(tmp_path):
	# The old reader stopped after 10 pages and refused every merge
	# (gate_unavailable); the stream reads page 11 and finds the hold.
	comments = _flood(1049) + [_claim(1050, "hold")]
	code, result, calls = _run_gate(tmp_path, _fixture(comments=comments))
	assert code == 1, result
	assert result["skip_reason"] == "hold_claim"
	assert result["claim"] == "held"
	assert len(_comment_page_calls(calls)) == 11


def test_comment_flood_without_a_trusted_hold_allows(tmp_path):
	# Untrusted comments that copy the hold marker never count, however many.
	fake_hold = f"<!-- ai:claude-fix-claim:v1 head={HEAD} kind=hold by=attacker -->"
	comments = (_flood(600, body=fake_hold)
		+ _flood(450, start_id=601, login=AUTHOR, association="CONTRIBUTOR", body=fake_hold))
	code, result, calls = _run_gate(tmp_path, _fixture(comments=comments))
	assert code == 0, result
	assert result["merge"] is True
	assert result["claim"] == "none"
	assert len(_comment_page_calls(calls)) == 11


def test_an_exact_page_multiple_reads_one_empty_page(tmp_path):
	comments = _flood(1099) + [_claim(1100, "hold")]
	code, result, calls = _run_gate(tmp_path, _fixture(comments=comments))
	assert code == 1, result
	assert result["skip_reason"] == "hold_claim"
	assert len(_comment_page_calls(calls)) == 12


def test_a_transient_page_failure_is_retried(tmp_path, monkeypatch, capsys):
	fixture = _fixture(comments=[_claim(1, "hold")])
	fixture["__flaky__"] = {COMMENTS_PATH: 2}
	code, result, calls, sleeps = _run_gate_in_process(tmp_path, monkeypatch, capsys, fixture)
	assert code == 1, result
	assert result["skip_reason"] == "hold_claim"
	assert len(_comment_page_calls(calls)) == 3
	assert sleeps == [1, 2]


def test_a_page_that_stays_unreadable_fails_closed_after_three_attempts(tmp_path, monkeypatch, capsys):
	code, result, calls, sleeps = _run_gate_in_process(tmp_path, monkeypatch, capsys, _fixture(fail=[COMMENTS_PATH]))
	assert code == 2
	assert result["merge"] is False
	assert result["skip_reason"] == "gate_unavailable"
	assert "after 3 attempts" in result["reason"]
	assert len(_comment_page_calls(calls)) == 3
	assert sleeps == [1, 2]


def test_zero_read_attempts_fail_closed_with_a_clear_reason(monkeypatch):
	check_in_status = gate._load_check_in_status()
	monkeypatch.setattr(gate, "COMMENT_READ_ATTEMPTS", 0)
	with pytest.raises(gate.GateUnavailable) as raised:
		gate._read_comment_page(check_in_status, COMMENTS_PATH, 1)
	assert str(raised.value).endswith("after 0 attempts: no read attempted")


def test_a_malformed_page_fails_closed_without_a_retry(tmp_path, monkeypatch, capsys):
	fixture = _fixture()
	fixture[COMMENTS_PATH] = {"message": "not a list"}
	code, result, calls, sleeps = _run_gate_in_process(tmp_path, monkeypatch, capsys, fixture)
	assert code == 2
	assert result["skip_reason"] == "gate_unavailable"
	assert len(_comment_page_calls(calls)) == 1
	assert sleeps == []


def test_a_trusted_comment_with_two_markers_is_still_ignored(tmp_path):
	two_markers = {**_claim(1, "hold"), "body": (f"<!-- ai:claude-fix-claim:v1 head={HEAD} kind=hold by=s1 -->\n"
		f"<!-- ai:claude-fix-claim:v1 head={OLD_HEAD} kind=hold by=s1 -->")}
	code, result, _ = _run_gate(tmp_path, _fixture(comments=[two_markers]))
	assert code == 0, result
	assert result["claim"] == "none"


def test_the_stream_keeps_only_trusted_marker_lines(monkeypatch):
	check_in_status = gate._load_check_in_status()
	marker = f"<!-- ai:claude-fix-claim:v1 head={HEAD} kind=hold by=session_x -->"
	pages = {
		1: _flood(99, body="x" * 10_000) + [{**_claim(100, "hold"), "body": "big text " * 1000 + "\n" + marker}],
		2: _flood(5, start_id=101, login=AUTHOR, association="OWNER", body="no marker here"),
	}
	requested = []

	def fake_read(path):
		requested.append(path)
		return pages[int(path.rsplit("page=", 1)[1])]

	monkeypatch.setattr(check_in_status, "_gh_api_json", fake_read)
	kept = gate._stream_claim_comments(check_in_status, REPO, 42, (AUTHOR.casefold(),))
	assert kept == [{"id": 100, "user": {"login": AUTHOR}, "author_association": "OWNER",
		"created_at": "2026-09-29T23:39:07Z", "body": marker}]
	assert requested == [f"{COMMENTS_PATH}?per_page=100&page=1", f"{COMMENTS_PATH}?per_page=100&page=2"]


# --- scripts/review_enable_auto_merge.sh -----------------------------------


# Issue #5565 round 3: the REST merge, never `gh pr merge`, which enables
# auto-merge or queues the PR on a merge-queue base even without `--auto`.
SYNC_MERGE = ["api", "-X", "PUT", f"repos/{REPO}/pulls/42/merge", "-f", "merge_method=squash", "-f", f"sha={HEAD}"]
AUTO_MERGE = ["pr", "merge", "42", "--repo", REPO, "--squash", "--auto", "--match-head-commit", HEAD]


def _is_merge_call(call):
	return call[:2] == ["pr", "merge"] or (call[:3] == ["api", "-X", "PUT"] and call[3].endswith("/merge"))


def _merged_rechecks(tmp_path):
	return [call for call in _calls(tmp_path) if "--jq" in call and ".merged == true" in call[call.index("--jq") + 1]]


def _run_helper(tmp_path, fixture, gate_script=GATE, extra_env=None):
	env = _fake_gh_env(tmp_path, fixture, {
		"GITHUB_REPOSITORY": REPO, "PR_NUMBER": "42", "ENABLE_AUTO_MERGE": "true",
		"FORWARD_MERGE_FALLBACK_AUTO_MERGE": "true", "ORCH_INTEGRATION_BRANCH_PATTERN": "^orchestrator/project-",
		"INITIAL_HEAD_SHA": HEAD, "GH_TOKEN": "fake", "GITHUB_ENV": str(tmp_path / "github_env.txt"),
		"GH_RETRY_MAX_ATTEMPTS": "1", "CLAUDE_MERGE_HOLD_GATE_SCRIPT": str(gate_script),
		**(extra_env or {}),
	})
	proc = subprocess.run(["bash", str(AUTO_MERGE_HELPER)], cwd=str(tmp_path), env=env, capture_output=True, text=True, check=False)
	merges = [call for call in _calls(tmp_path) if _is_merge_call(call)]
	labels_env = (tmp_path / "github_env.txt").read_text(encoding="utf-8").splitlines()
	return proc, merges, labels_env


def test_helper_skips_the_merge_for_a_held_claude_head(tmp_path):
	proc, merges, labels_env = _run_helper(tmp_path, _fixture(comments=[_claim(1, "hold")]))
	assert proc.returncode == 0, proc.stderr
	assert merges == []
	assert f"AUTOFIX_AUTO_MERGE_SKIPPED pr=42 head_sha={HEAD} reason=hold_claim" in proc.stdout, proc.stdout
	assert labels_env == ["AUTO_MERGE_READY_LABELS_ALLOWED=false"]


def test_helper_skips_the_merge_for_a_twin_parity_break(tmp_path):
	fixture = _fixture(files=[TWIN], head_blobs={TWIN: "t2", LIVE: "t1"}, base_blobs={TWIN: "t1", LIVE: "t1"})
	proc, merges, _ = _run_helper(tmp_path, fixture)
	assert merges == []
	assert "reason=twin_parity" in proc.stdout, proc.stdout


def test_helper_merges_a_clean_claude_head_synchronously(tmp_path):
	# Issue #5565: no auto-merge enrollment, which would merge after pending
	# checks settle without running the gate again.
	proc, merges, labels_env = _run_helper(tmp_path, _fixture(comments=[_claim(1, "hold", head=OLD_HEAD)]))
	assert proc.returncode == 0, proc.stderr
	assert merges == [SYNC_MERGE]
	assert f"AUTOFIX_MERGE_HOLD_GATE pr=42 head_sha={HEAD} action=allow" in proc.stdout
	assert f"AUTOFIX_AUTO_MERGE_HEAD_BOUND pr=42 head_sha={HEAD} action=squash_sync" in proc.stdout
	assert labels_env[-1] == "AUTO_MERGE_READY_LABELS_ALLOWED=true"


def test_helper_refused_sync_merge_enrolls_nothing(tmp_path):
	# Required checks still pending: GitHub refuses the synchronous merge, and
	# the helper must not fall back to --auto.
	proc, merges, labels_env = _run_helper(tmp_path, _fixture(), extra_env={"FAKE_GH_MERGE_FAIL": "1"})
	assert proc.returncode == 0, proc.stderr
	assert merges == [SYNC_MERGE]
	assert len(_merged_rechecks(tmp_path)) == 1
	assert f"AUTOFIX_AUTO_MERGE_SKIPPED pr=42 head_sha={HEAD} reason=merge_not_ready" in proc.stdout, proc.stdout
	assert labels_env == ["AUTO_MERGE_READY_LABELS_ALLOWED=false"]


def test_helper_counts_a_lost_merge_response_as_merged(tmp_path):
	# The merge landed but the response was lost; the retry is refused because
	# the PR is already merged. The one re-read sees it merged at this head.
	proc, merges, labels_env = _run_helper(tmp_path, _fixture(), extra_env={
		"FAKE_GH_MERGE_FAIL": "1", "FAKE_GH_MERGED_AT": f"true {HEAD}"})
	assert proc.returncode == 0, proc.stderr
	assert merges == [SYNC_MERGE]
	assert "reason=merge_not_ready" not in proc.stdout, proc.stdout
	assert labels_env[-1] == "AUTO_MERGE_READY_LABELS_ALLOWED=true"


def test_helper_does_not_count_a_merge_at_another_head(tmp_path):
	proc, _, labels_env = _run_helper(tmp_path, _fixture(), extra_env={
		"FAKE_GH_MERGE_FAIL": "1", "FAKE_GH_MERGED_AT": f"true {OLD_HEAD}"})
	assert proc.returncode == 0, proc.stderr
	assert f"AUTOFIX_AUTO_MERGE_SKIPPED pr=42 head_sha={HEAD} reason=merge_not_ready" in proc.stdout, proc.stdout
	assert labels_env == ["AUTO_MERGE_READY_LABELS_ALLOWED=false"]


def test_helper_never_runs_gh_pr_merge_for_a_claude_head(tmp_path):
	# On a merge-queue base `gh pr merge` enrolls auto-merge or queues the PR
	# even without --auto, so the claude/* path must not call it at all.
	for env in ({}, {"FAKE_GH_MERGE_FAIL": "1"}):
		run_dir = tmp_path / str(len(env))
		run_dir.mkdir()
		_run_helper(run_dir, _fixture(), extra_env=env)
		assert not [call for call in _calls(run_dir) if call[:2] == ["pr", "merge"]]


def test_helper_never_runs_the_gate_for_other_heads(tmp_path):
	proc, merges, _ = _run_helper(tmp_path, _fixture(pr=_pr(ref="ai/issue-42"), comments=[_claim(1, "hold")]))
	assert merges == [AUTO_MERGE]
	assert not [call for call in _calls(tmp_path) if "/comments" in " ".join(call)]
	assert "AUTOFIX_MERGE_HOLD_GATE" not in proc.stdout


def test_helper_refuses_when_the_gate_script_is_missing(tmp_path):
	proc, merges, _ = _run_helper(tmp_path, _fixture(), gate_script=tmp_path / "missing.py")
	assert merges == []
	assert "reason=gate_unavailable" in proc.stdout, proc.stdout


def test_helper_gate_runs_before_every_merge_call():
	text = AUTO_MERGE_HELPER.read_text(encoding="utf-8")
	gate_at = text.index('"${_hold_gate_script}" --repo')
	assert gate_at < text.index("gh_retry gh pr merge"), "the hold gate must precede every merge call"
	assert gate_at < text.index("gh_retry gh api -X PUT"), "the hold gate must precede the claude/* merge"
	assert text.index("no longer points at reviewed head") < gate_at, "the gate runs after the head freshness check"


def test_helper_claude_branch_exits_before_the_auto_merge_tail():
	text = AUTO_MERGE_HELPER.read_text(encoding="utf-8")
	sync_at = text.index('gh api -X PUT "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/merge" -f merge_method=squash -f sha="${INITIAL_HEAD_SHA}"')
	claude_branch_at = text.rindex('if [[ "${_orch_pr_head_ref}" == claude/* ]]; then', 0, sync_at)
	tail_at = text.index('--repo "${GITHUB_REPOSITORY}" --squash --auto --match-head-commit')
	assert text.index('"${_hold_gate_script}" --repo') < claude_branch_at < sync_at < tail_at
	assert "\texit 0\nfi" in text[sync_at:tail_at], "the claude/* branch must exit before the --auto tail"


# --- .github/workflows/review_autofix.yml wiring ---------------------------


def _jobs():
	return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job, name):
	return next(step for step in job["steps"] if step.get("name") == name)


def test_enable_auto_merge_step_passes_the_trusted_login():
	step = _step(_jobs()["codex-agent"], "Enable auto-merge on PR")
	assert step["env"]["CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN"] == "${{ vars.CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN || '' }}"
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/review_enable_auto_merge.sh"' in step["run"]


def test_deterministic_skip_checks_out_the_gate_for_claude_heads():
	job = _jobs()["deterministic-skip-merge"]
	assert job["env"]["CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN"] == "${{ vars.CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN || '' }}"
	checkout = _step(job, "Checkout merge hold gate support source")
	assert checkout["if"] == "${{ startsWith(needs.gate.outputs.head_ref, 'claude/') }}"
	assert checkout["with"]["ref"] == "${{ needs.gate.outputs.review_support_sha }}"
	assert checkout["with"]["path"] == ".codex-workflow-src"
	assert set(checkout["with"]["sparse-checkout"].split()) == {"scripts/claude_merge_hold_gate.py", ".claude/scripts/check_in_status.py"}
	verify = _step(job, "Verify merge hold gate support source")
	assert verify["if"] == checkout["if"]
	assert "git -C .codex-workflow-src rev-parse HEAD" in verify["run"]
	names = [step.get("name") for step in job["steps"]]
	assert names.index("Verify merge hold gate support source") < names.index("Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge")


def test_deterministic_skip_runs_the_gate_before_every_merge_call():
	run = _step(_jobs()["deterministic-skip-merge"], "Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge")["run"]
	branch = 'elif [[ "${PR_HEAD_REF}" == claude/* ]] && ! merge_hold_gate_allows; then'
	assert branch in run
	assert run.index(branch) < run.index("gh_retry gh pr merge"), "the hold gate branch must precede every merge call"
	assert run.index(branch) < run.index("gh_retry gh api -X PUT"), "the hold gate branch must precede the claude/* merge"
	assert ".codex-workflow-src/scripts/claude_merge_hold_gate.py" in run
	assert "AUTOFIX_AUTO_MERGE_SKIPPED pr=${PR_NUMBER} head_sha=${PR_HEAD_SHA}" in run


def _run_deterministic_skip_step(tmp_path, fixture, head_ref="claude/implement-plan-x-phase-1", extra_env=None):
	run = _step(_jobs()["deterministic-skip-merge"], "Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge")["run"]
	support = tmp_path / ".codex-workflow-src"
	(support / "scripts").mkdir(parents=True)
	(support / ".claude" / "scripts").mkdir(parents=True)
	(support / "scripts" / "claude_merge_hold_gate.py").write_bytes(GATE.read_bytes())
	(support / ".claude" / "scripts" / "check_in_status.py").write_bytes((ROOT / ".claude" / "scripts" / "check_in_status.py").read_bytes())
	env = _fake_gh_env(tmp_path, fixture, {
		"REPOSITORY": REPO, "PR_NUMBER": "42", "DET_SKIP_REASON": "doc_only", "ENABLE_AUTO_MERGE": "true",
		"FORWARD_MERGE_FALLBACK_AUTO_MERGE": "true", "PR_HEAD_REF": head_ref, "PR_HEAD_SHA": HEAD,
		"GH_TOKEN": "fake", "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
		**(extra_env or {}),
	})
	proc = subprocess.run(["bash", "-c", run], cwd=str(tmp_path), env=env, capture_output=True, text=True, check=False)
	return proc, [call for call in _calls(tmp_path) if _is_merge_call(call)]


def test_deterministic_skip_refuses_a_held_claude_head(tmp_path):
	proc, merges = _run_deterministic_skip_step(tmp_path, _fixture(comments=[_claim(1, "hold")]))
	assert proc.returncode == 0, proc.stderr
	assert merges == []
	assert "reason=hold_claim" in proc.stdout, proc.stdout
	assert "REFUSED (merge hold gate" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_deterministic_skip_merges_an_unheld_claude_head_synchronously(tmp_path):
	proc, merges = _run_deterministic_skip_step(tmp_path, _fixture())
	assert proc.returncode == 0, proc.stderr
	assert merges == [SYNC_MERGE]
	assert f"AUTOFIX_DET_SKIP_MERGE_BOUND pr=42 head_sha={HEAD} action=squash_sync" in proc.stdout
	assert "MERGED (claude/* synchronous merge)" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_deterministic_skip_refused_sync_merge_enrolls_nothing(tmp_path):
	# The step's inline gh_retry retries four times; a no-op `sleep` on PATH
	# keeps the backoff out of the test.
	(tmp_path / "bin").mkdir(exist_ok=True)
	(tmp_path / "bin" / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
	(tmp_path / "bin" / "sleep").chmod(0o755)
	proc, merges = _run_deterministic_skip_step(tmp_path, _fixture(), extra_env={"FAKE_GH_MERGE_FAIL": "1"})
	assert proc.returncode == 0, proc.stderr
	assert merges and all(merge == SYNC_MERGE for merge in merges), merges
	assert f"AUTOFIX_AUTO_MERGE_SKIPPED pr=42 head_sha={HEAD} reason=merge_not_ready" in proc.stdout, proc.stdout
	assert "REFUSED (merge_not_ready" in (tmp_path / "summary.md").read_text(encoding="utf-8")
	assert len(_merged_rechecks(tmp_path)) == 1
	label_calls = [call for call in _calls(tmp_path) if "/labels" in " ".join(call)]
	assert label_calls == [], "a refused merge must not add ai:review-skipped or ai:ready-to-merge"
	assert not [call for call in _calls(tmp_path) if call[:2] == ["pr", "merge"]]


def test_deterministic_skip_counts_a_lost_merge_response_as_merged(tmp_path):
	(tmp_path / "bin").mkdir(exist_ok=True)
	(tmp_path / "bin" / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
	(tmp_path / "bin" / "sleep").chmod(0o755)
	proc, merges = _run_deterministic_skip_step(tmp_path, _fixture(), extra_env={
		"FAKE_GH_MERGE_FAIL": "1", "FAKE_GH_MERGED_AT": f"true {HEAD}"})
	assert proc.returncode == 0, proc.stderr
	assert merges and all(merge == SYNC_MERGE for merge in merges), merges
	assert "reason=merge_not_ready" not in proc.stdout, proc.stdout
	assert "MERGED (claude/* synchronous merge)" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_deterministic_skip_keeps_auto_merge_for_other_heads(tmp_path):
	proc, merges = _run_deterministic_skip_step(tmp_path, _fixture(pr=_pr(ref="ai/issue-42")), head_ref="ai/issue-42")
	assert proc.returncode == 0, proc.stderr
	assert merges == [AUTO_MERGE]


# --- agents.md stable log prefix registry ----------------------------------


@pytest.mark.parametrize("prefix", ["AUTOFIX_AUTO_MERGE_SKIPPED", "AUTOFIX_MERGE_HOLD_GATE"])
def test_gate_log_keys_are_registered_stable_prefixes(prefix):
	agents_text = (ROOT / "agents.md").read_text(encoding="utf-8")
	assert f"- `{prefix}`" in agents_text, prefix
	assert f"LOG_PREFIX.name={prefix}" in agents_text, prefix
	assert f"{prefix} pr=" in AUTO_MERGE_HELPER.read_text(encoding="utf-8"), prefix
	assert f"{prefix} pr=" in WORKFLOW.read_text(encoding="utf-8"), prefix


# --- gate job: stale auto-merge enrollment cancel (issue #5565) ------------

CANCEL_BLOCK_START = "          # claude/* PRs are merged synchronously after the hold gate and never\n"


def _gate_evaluate_run():
	return next(step for step in _jobs()["gate"]["steps"] if step.get("id") == "evaluate")["run"]


def _cancel_block():
	run = _gate_evaluate_run()
	start = run.index(CANCEL_BLOCK_START.strip())
	end = run.index('if [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]] && [ -z "${pr_state}" ]; then', start)
	return run[start:end]


def test_gate_pr_fetch_reports_the_auto_merge_enrollment():
	run = _gate_evaluate_run()
	assert "auto_merge: (.auto_merge != null)" in run
	assert 'pr_auto_merge="false"' in run
	assert run.index("auto_merge: (.auto_merge != null)") < run.index(CANCEL_BLOCK_START.strip())


def _run_cancel_block(tmp_path, state="open", auto_merge="true", head_ref="claude/implement-plan-x-phase-1", fail=False, gone=False):
	extra = {"PR_NUMBER": "42", "REPOSITORY": REPO, "pr_state": state, "pr_auto_merge": auto_merge,
		"pr_head_ref": head_ref, "pr_head_sha_gate": HEAD}
	if fail:
		extra["FAKE_GH_DISABLE_FAIL"] = "1"
	if gone:
		extra["FAKE_GH_AUTO_MERGE_GONE"] = "1"
	env = _fake_gh_env(tmp_path, _fixture(), extra)
	# The retry backoff sleeps; record the delays instead of waiting.
	sleep_stub = tmp_path / "bin" / "sleep"
	sleep_stub.write_text(f'#!/bin/sh\necho "$1" >> "{tmp_path / "sleeps.txt"}"\n', encoding="utf-8")
	sleep_stub.chmod(0o755)
	script = "set -euo pipefail\n" + textwrap.dedent(_cancel_block())
	proc = subprocess.run(["bash", "-c", script], cwd=str(tmp_path), env=env, capture_output=True, text=True, check=False)
	return proc, [call for call in _calls(tmp_path) if call[:2] == ["pr", "merge"]]


def _cancel_rechecks(tmp_path):
	return [call for call in _calls(tmp_path) if call[:2] == ["api", f"repos/{REPO}/pulls/42"]]


def test_gate_cancels_an_enrollment_on_an_open_claude_pr(tmp_path):
	proc, merges = _run_cancel_block(tmp_path)
	assert proc.returncode == 0, proc.stderr
	assert merges == [["pr", "merge", "42", "--repo", REPO, "--disable-auto"]]
	assert _cancel_rechecks(tmp_path) == []
	assert f"AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED pr=42 head_sha={HEAD} result=disabled" in proc.stdout


def test_gate_cancel_failure_warns_and_continues(tmp_path):
	proc, merges = _run_cancel_block(tmp_path, fail=True)
	assert proc.returncode == 0, proc.stderr
	assert merges == [["pr", "merge", "42", "--repo", REPO, "--disable-auto"]] * 3
	# After each failed attempt one REST read checks whether the enrollment is gone.
	assert len(_cancel_rechecks(tmp_path)) == 3
	assert (tmp_path / "sleeps.txt").read_text(encoding="utf-8").split() == ["2", "4"]
	assert proc.stdout.count("AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED") == 1
	assert f"AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED pr=42 head_sha={HEAD} result=failed" in proc.stdout
	warning = next(line for line in proc.stdout.splitlines() if line.startswith("::warning::"))
	assert "HTTP 502: Bad Gateway (https://api.github.com/graphql)" in warning
	assert "second line" not in proc.stdout


def test_gate_cancel_counts_a_lost_response_as_disabled(tmp_path):
	# The mutation landed but gh reported an error: the re-check sees no enrollment.
	proc, merges = _run_cancel_block(tmp_path, fail=True, gone=True)
	assert proc.returncode == 0, proc.stderr
	assert merges == [["pr", "merge", "42", "--repo", REPO, "--disable-auto"]]
	assert len(_cancel_rechecks(tmp_path)) == 1
	assert not (tmp_path / "sleeps.txt").exists()
	assert f"AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED pr=42 head_sha={HEAD} result=disabled" in proc.stdout
	assert "::warning::" not in proc.stdout


@pytest.mark.parametrize("state,auto_merge,head_ref", [
	("open", "false", "claude/implement-plan-x-phase-1"),
	("closed", "true", "claude/implement-plan-x-phase-1"),
	("open", "true", "ai/issue-42"),
])
def test_gate_leaves_other_prs_alone(tmp_path, state, auto_merge, head_ref):
	proc, merges = _run_cancel_block(tmp_path, state=state, auto_merge=auto_merge, head_ref=head_ref)
	assert proc.returncode == 0, proc.stderr
	assert merges == []
	assert _cancel_rechecks(tmp_path) == []
	assert "AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED" not in proc.stdout


def test_claude_heads_never_reach_an_auto_merge_enrollment_in_the_deterministic_skip_step():
	run = _step(_jobs()["deterministic-skip-merge"], "Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge")["run"]
	sync_branch = 'elif [[ "${PR_HEAD_REF}" == claude/* ]]; then'
	assert run.index('elif [[ "${PR_HEAD_REF}" == claude/* ]] && ! merge_hold_gate_allows; then') < run.index(sync_branch)
	branch = run[run.index(sync_branch):run.index("\nelse\n", run.index(sync_branch))]
	assert 'gh api -X PUT "repos/${REPOSITORY}/pulls/${PR_NUMBER}/merge" -f merge_method=squash -f sha="${PR_HEAD_SHA}"' in branch
	assert "--auto" not in branch
	assert "gh_retry gh pr merge" not in branch


def test_cancel_log_key_is_a_registered_stable_prefix():
	agents_text = (ROOT / "agents.md").read_text(encoding="utf-8")
	assert "- `AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED`" in agents_text
	assert "LOG_PREFIX.name=AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED" in agents_text
	assert "AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED pr=" in WORKFLOW.read_text(encoding="utf-8")
