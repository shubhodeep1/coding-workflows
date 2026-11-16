"""Exercise the SHA-bound status and stale auto-merge gate without GitHub."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "review_head_gate.sh"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps(args) + "\\n")
if args[:2] == ["api", "repos/o/r/pulls/42"]:
    if os.environ.get("FAKE_FAIL") == "read": sys.exit(1)
    print(json.dumps({"state": "open", "head": {"sha": os.environ.get("FAKE_HEAD", "a" * 40)},
        "auto_merge": {"enabled_by": {"login": "bot"}} if os.environ.get("FAKE_ENABLED") == "true" else None}))
if "--disable-auto" in args and os.environ.get("FAKE_FAIL") == "disable": sys.exit(1)
if "statuses/" in " ".join(args) and os.environ.get("FAKE_FAIL") == "status": sys.exit(1)
'''


def _run(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh_path = bin_dir / "gh"
	gh_path.write_text(FAKE_GH, encoding="utf-8")
	gh_path.chmod(0o755)
	log_path = tmp_path / "gh.log"
	env = {
		**os.environ,
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"FAKE_GH_LOG": str(log_path),
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"REPOSITORY": "o/r",
		"PR_NUMBER": "42",
		"EVENT_NAME": "pull_request",
		"EVENT_ACTION": "synchronize",
		"PR_EVENT_HEAD_SHA": HEAD,
		"GATE_HEAD_SHA": HEAD,
		"GATE_AUTO_MERGE_ENABLED": "true",
		"DETERMINISTIC_SKIP": "false",
		"REVIEW_HEAD_GATE_STATUS_ENABLED": "true",
		"REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED": "true",
		**overrides,
	}
	result = subprocess.run(["bash", str(HELPER), "gate"], env=env, capture_output=True, text=True, check=False)
	calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.exists() else []
	return result, calls


def _statuses(calls: list[list[str]]) -> list[list[str]]:
	return [call for call in calls if any("/statuses/" in arg for arg in call)]


def test_synchronize_posts_pending_then_withdraws(tmp_path: Path) -> None:
	result, calls = _run(tmp_path)
	assert result.returncode == 0, result.stderr
	assert len(_statuses(calls)) == 1
	assert calls[0][:4] == ["api", "-X", "POST", f"repos/o/r/statuses/{HEAD}"]
	assert "context=ai-review/head-gate" in calls[0]
	assert "state=pending" in calls[0]
	assert calls[1] == ["pr", "merge", "42", "--repo", "o/r", "--disable-auto"]


@pytest.mark.parametrize("known,expected_read,expected_disable", [("false", False, False), ("", True, True)])
def test_cached_or_unknown_enrollment(tmp_path: Path, known: str, expected_read: bool, expected_disable: bool) -> None:
	if known == "" and shutil.which("jq") is None:
		pytest.skip("jq is required for live PR response validation")
	result, calls = _run(tmp_path, GATE_AUTO_MERGE_ENABLED=known, FAKE_ENABLED="true")
	assert result.returncode == 0
	assert any(call[:2] == ["api", "repos/o/r/pulls/42"] for call in calls) == expected_read
	assert any("--disable-auto" in call for call in calls) == expected_disable


def test_disable_failure_fails_gate(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, FAKE_FAIL="disable", DETERMINISTIC_SKIP="true")
	assert result.returncode != 0
	assert "outcome=failed reason=disable_failed" in result.stdout
	assert not any("state=success" in call for call in _statuses(calls))


@pytest.mark.parametrize("action,expected", [("opened", 1), ("reopened", 0), ("synchronize", 1)])
def test_event_action(tmp_path: Path, action: str, expected: int) -> None:
	result, calls = _run(tmp_path, EVENT_ACTION=action, GATE_AUTO_MERGE_ENABLED="false")
	assert result.returncode == 0
	assert len(_statuses(calls)) == expected
	assert not any("--disable-auto" in call for call in calls)


def test_dispatch_skip_status_is_bound_to_gate_head(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, EVENT_NAME="workflow_dispatch", EVENT_ACTION="", DETERMINISTIC_SKIP="true", GATE_HEAD_SHA=NEW_HEAD)
	assert result.returncode == 0
	assert len(_statuses(calls)) == 1
	assert f"repos/o/r/statuses/{NEW_HEAD}" in calls[0]
	assert "state=success" in calls[0]
	assert not any("--disable-auto" in call for call in calls)


def test_changed_event_head_marks_both_heads_pending(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, PR_EVENT_HEAD_SHA=NEW_HEAD, GATE_AUTO_MERGE_ENABLED="false")
	assert result.returncode == 0
	assert {call[3] for call in _statuses(calls)} == {f"repos/o/r/statuses/{HEAD}", f"repos/o/r/statuses/{NEW_HEAD}"}


@pytest.mark.parametrize("overrides,expected", [
	({"GATE_HEAD_SHA": "invalid", "PR_EVENT_HEAD_SHA": "invalid", "GATE_AUTO_MERGE_ENABLED": "false"}, 0),
	({"REVIEW_HEAD_GATE_STATUS_ENABLED": "false", "GATE_AUTO_MERGE_ENABLED": "false"}, 0),
	({"FAKE_FAIL": "status", "GATE_AUTO_MERGE_ENABLED": "false"}, 1),
	({"REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED": "false"}, 1),
])
def test_status_and_withdraw_switches(tmp_path: Path, overrides: dict[str, str], expected: int) -> None:
	result, calls = _run(tmp_path, **overrides)
	assert result.returncode == 0
	assert len(_statuses(calls)) == expected
	if overrides.get("REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED") == "false":
		assert not any("--disable-auto" in call for call in calls)


# --- freshness-sync: may an update-branch head skip the reviewers? (#7084, Q60) ---

APPROVED = "c" * 40
MAIN_TIP = "d" * 40
MERGE_HEAD = "e" * 40
FRESHNESS_FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps(args) + "\\n")
path = args[1] if len(args) > 1 else ""
if path.startswith("repos/o/r/commits/"):
    if os.environ.get("FAKE_FAIL") == "commit": sys.exit(1)
    print(os.environ["FAKE_COMMIT"])
    sys.exit(0)
if path.startswith("repos/o/r/compare/"):
    if os.environ.get("FAKE_FAIL") == "compare": sys.exit(1)
    print(os.environ.get("FAKE_COMPARE_STATUS", "ahead"))
    sys.exit(0)
sys.exit(1)
'''


def _marker(sync: int = 1, *, pr: int = 42, frm: str = APPROVED, author: str = "workflow-bot", base: str | None = MAIN_TIP, merge: str | None = MERGE_HEAD) -> dict:
	bound = f" base={base} merge={merge}" if base is not None and merge is not None else ""
	return {"author_login": author, "body": f"**Branch updated from `main` before merging** (update {sync}; warning threshold 2).\n\n<!-- ai:merge-base-sync:v1 pr={pr} from={frm} sync={sync}{bound} -->"}


def _commit(parents: list[str] | None = None, *, verified: bool = True, committer: str = "web-flow") -> str:
	return json.dumps({"parents": parents if parents is not None else [APPROVED, MAIN_TIP], "verified": verified, "committer": committer})


def _freshness(tmp_path: Path, comments: list[dict], *, commit: str | None = None, **overrides: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh_path = bin_dir / "gh"
	gh_path.write_text(FRESHNESS_FAKE_GH, encoding="utf-8")
	gh_path.chmod(0o755)
	log_path = tmp_path / "gh.log"
	comments_file = tmp_path / "comments.json"
	comments_file.write_text(json.dumps(comments), encoding="utf-8")
	env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GH_LOG": str(log_path),
		"GH_RETRY_MAX_ATTEMPTS": "1", "FAKE_COMMIT": commit or _commit(), **overrides}
	result = subprocess.run(["bash", str(HELPER), "freshness-sync", "o/r", "42", MERGE_HEAD, "main", "workflow-bot", str(comments_file)],
		env=env, capture_output=True, text=True, check=False)
	calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.exists() else []
	return result, calls


def test_github_merge_of_the_approved_head_with_main_skips_review(tmp_path: Path) -> None:
	result, calls = _freshness(tmp_path, [_marker()])
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"REVIEW_HEAD_GATE mode=freshness_sync pr=42 head={MERGE_HEAD} outcome=skip reason=freshness_sync from={APPROVED} base_parent={MAIN_TIP} sync=1 merge={MERGE_HEAD}" in result.stdout
	assert [call[1] for call in calls] == [f"repos/o/r/commits/{MERGE_HEAD}", f"repos/o/r/compare/{MAIN_TIP}...main"]


def test_the_newest_marker_by_the_workflow_login_wins(tmp_path: Path) -> None:
	comments = [_marker(1, frm="f" * 40), _marker(2, frm=APPROVED), _marker(3, frm="9" * 40, author="intruder"), _marker(4, frm="8" * 40, pr=43),
		_marker(5, frm="7" * 40, merge="6" * 40)]
	result, _calls = _freshness(tmp_path, comments)
	assert result.returncode == 0, result.stdout
	assert f"from={APPROVED}" in result.stdout and "sync=2" in result.stdout


@pytest.mark.parametrize("comments,reason", (
	([], "no_sync_marker"),
	([_marker(author="someone-else")], "no_sync_marker"),
	([_marker(pr=43)], "no_sync_marker"),
	([{"author_login": "workflow-bot", "body": f"quoted: <!-- ai:merge-base-sync:v1 pr=42 from={APPROVED} sync=1 --> and more"}], "no_sync_marker"),
	([{"author_login": "workflow-bot", "body": f"AI autofix editor summary\n\n<!-- ai:merge-base-sync:v1 pr=42 from={APPROVED} sync=1 -->"}], "no_sync_marker"),
))
def test_no_trusted_marker_means_a_normal_review_and_no_api_call(tmp_path: Path, comments: list[dict], reason: str) -> None:
	result, calls = _freshness(tmp_path, comments)
	assert result.returncode == 1
	assert f"outcome=review reason={reason}" in result.stdout
	assert calls == []


@pytest.mark.parametrize("commit,reason", (
	(_commit([APPROVED]), "head_is_not_the_update"),
	(_commit([APPROVED, MAIN_TIP, "1" * 40]), "head_is_not_the_update"),
	(_commit(["f" * 40, MAIN_TIP]), "head_is_not_the_update"),
	(_commit(verified=False), "merge_not_made_by_github"),
	(_commit(committer="shubhodeep1"), "merge_not_made_by_github"),
))
def test_a_head_that_is_not_githubs_merge_of_the_approved_head_is_reviewed(tmp_path: Path, commit: str, reason: str) -> None:
	result, calls = _freshness(tmp_path, [_marker()], commit=commit)
	assert result.returncode == 1
	assert f"outcome=review reason={reason}" in result.stdout
	assert not any("/compare/" in call[1] for call in calls)


@pytest.mark.parametrize("status", ("behind", "diverged", ""))
def test_a_second_parent_that_is_not_on_the_base_is_reviewed(tmp_path: Path, status: str) -> None:
	result, _calls = _freshness(tmp_path, [_marker()], FAKE_COMPARE_STATUS=status)
	assert result.returncode == 1
	assert "outcome=review reason=base_parent_not_on_base" in result.stdout


@pytest.mark.parametrize("fail,reason", (("commit", "commit_read_failed"), ("compare", "base_compare_failed")))
def test_api_failures_fall_back_to_a_normal_review(tmp_path: Path, fail: str, reason: str) -> None:
	result, _calls = _freshness(tmp_path, [_marker()], FAKE_FAIL=fail)
	assert result.returncode == 1
	assert f"outcome=review reason={reason}" in result.stdout


def test_invalid_inputs_are_reviewed(tmp_path: Path) -> None:
	comments_file = tmp_path / "c.json"
	comments_file.write_text("[]", encoding="utf-8")
	for args in (["o/r", "0", MERGE_HEAD, "main", "bot", str(comments_file)], ["o/r", "42", "abc", "main", "bot", str(comments_file)],
			["o/r", "42", MERGE_HEAD, "a..b", "bot", str(comments_file)], ["o/r", "42", MERGE_HEAD, "main", "", str(comments_file)],
			["o/r", "42", MERGE_HEAD, "main", "bot", str(tmp_path / "missing.json")]):
		result = subprocess.run(["bash", str(HELPER), "freshness-sync", *args], capture_output=True, text=True, check=False)
		assert result.returncode == 1, args
		assert "outcome=review reason=invalid_input" in result.stdout


def test_a_legacy_marker_without_base_and_merge_never_skips(tmp_path: Path) -> None:
	result, calls = _freshness(tmp_path, [_marker(base=None, merge=None)])
	assert result.returncode == 1
	assert "outcome=review reason=unbound_marker" in result.stdout
	assert calls == []


def test_a_bound_marker_for_another_commit_does_not_cover_this_head(tmp_path: Path) -> None:
	result, calls = _freshness(tmp_path, [_marker(merge="6" * 40)])
	assert result.returncode == 1
	assert "outcome=review reason=marker_not_for_head" in result.stdout
	assert calls == []


def test_a_signed_github_merge_with_a_different_base_parent_is_reviewed(tmp_path: Path) -> None:
	# Attack shape (finding freshness-sync-signed-merge-review-bypass): the
	# branch is reset to the approved head and a web conflict-resolver merge
	# [approved, other base tip] is made. Even if its SHA were recorded, the
	# base parent differs from the one the marker bound.
	other_tip = "1" * 40
	result, calls = _freshness(tmp_path, [_marker()], commit=_commit([APPROVED, other_tip]))
	assert result.returncode == 1
	assert "outcome=review reason=base_parent_mismatch" in result.stdout
	assert not any("/compare/" in call[1] for call in calls)


def test_a_conflict_resolver_merge_on_a_reset_branch_is_not_the_recorded_commit(tmp_path: Path) -> None:
	result, calls = _freshness(tmp_path, [_marker(merge="2" * 40)], commit=_commit([APPROVED, "3" * 40]))
	assert result.returncode == 1
	assert "outcome=review reason=marker_not_for_head" in result.stdout
	assert calls == []


# --- freshness-sync after CI failed on the head (Q103, PR #7153) ---

def _ci_failed_marker(*, head: str = MERGE_HEAD, pr: int = 42, author: str = "workflow-bot") -> dict:
	return {"author_login": author, "body": f"**CI failed on the updated head `{head[:12]}`.** ...\n\n<!-- ai:freshness-ci-failed:v1 pr={pr} head={head} -->"}


def test_a_head_whose_ci_failed_is_reviewed_without_api_calls(tmp_path: Path) -> None:
	result, calls = _freshness(tmp_path, [_marker(), _ci_failed_marker()])
	assert result.returncode == 1
	assert f"REVIEW_HEAD_GATE mode=freshness_sync pr=42 head={MERGE_HEAD} outcome=review reason=ci_failed_on_head from={APPROVED} sync=1" in result.stdout
	assert calls == []


@pytest.mark.parametrize("marker", (
	_ci_failed_marker(head="f" * 40),
	_ci_failed_marker(pr=43),
	_ci_failed_marker(author="intruder"),
))
def test_a_ci_failed_marker_for_another_head_pr_or_author_is_ignored(tmp_path: Path, marker: dict) -> None:
	result, _calls = _freshness(tmp_path, [_marker(), marker])
	assert result.returncode == 0, result.stdout + result.stderr
	assert "reason=freshness_sync" in result.stdout


# ── Updater-PR exemption (#9483) ─────────────────────────────────────────


UPDATER_REF = "auto/update-workflows-0123456789ab"
UPDATER_FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps(args) + "\\n")
joined = " ".join(args)
if "/statuses/" in joined:
    sys.exit(1 if os.environ.get("FAKE_FAIL") == "post" else 0)
if args[:2] == ["api", "repos/o/r/pulls/42"]:
    if os.environ.get("FAKE_FAIL") == "read": sys.exit(1)
    print(os.environ["FAKE_PR_JSON"]); sys.exit(0)
if len(args) >= 2 and args[0] == "api" and args[1].startswith("repos/o/r/commits/") and "/status" in args[1]:
    if os.environ.get("FAKE_FAIL") == "status_read": sys.exit(1)
    print(os.environ["FAKE_STATUS_JSON"]); sys.exit(0)
sys.exit(1)
'''


def _updater_pr(**overrides: object) -> dict:
	pr = {
		"state": "open",
		"head": {"ref": UPDATER_REF, "sha": HEAD, "repo": {"full_name": "o/r"}},
		"base": {"ref": "main", "repo": {"full_name": "o/r"}},
		"auto_merge": None,
	}
	pr.update(overrides)
	return pr


def _status(verify: str | None = "success", integrity: str | None = "success", total: int | None = None) -> dict:
	statuses = []
	if verify is not None:
		statuses.append({"context": "ai-update-workflows/verify", "state": verify})
	if integrity is not None:
		statuses.append({"context": "coding-workflows/workflow-integrity", "state": integrity})
	statuses.append({"context": "ai-review/head-gate", "state": "pending"})
	return {"state": "pending", "total_count": len(statuses) if total is None else total, "statuses": statuses}


def _run_exempt(tmp_path: Path, hint: str = UPDATER_REF, pr: object = None, status: object = None,
	pass_json: bool = False, **env_overrides: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
	if shutil.which("jq") is None:
		pytest.skip("jq is required by the updater exemption")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh_path = bin_dir / "gh"
	gh_path.write_text(UPDATER_FAKE_GH, encoding="utf-8")
	gh_path.chmod(0o755)
	log_path = tmp_path / "gh.log"
	pr_json = json.dumps(_updater_pr() if pr is None else pr)
	env = {
		**{k: v for k, v in os.environ.items() if not k.startswith(("REVIEW_HEAD_GATE_", "FAKE_"))},
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"FAKE_GH_LOG": str(log_path),
		"FAKE_PR_JSON": pr_json,
		"FAKE_STATUS_JSON": json.dumps(_status() if status is None else status),
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"PR_NUMBER": "42",
		**env_overrides,
	}
	script = (
		f'source "{ROOT}/scripts/gh_helpers.sh"; source "{HELPER}"; '
		f'review_head_gate_updater_exempt o/r 42 "{HEAD}" "$1" "$2"'
	)
	result = subprocess.run(
		["bash", "-c", script, "exempt", hint, pr_json if pass_json else ""],
		env=env, capture_output=True, text=True, check=False,
	)
	calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.exists() else []
	return result, calls


def test_updater_pr_with_green_verify_and_integrity_gets_success(tmp_path: Path) -> None:
	result, calls = _run_exempt(tmp_path)
	assert result.returncode == 0, result.stderr
	posts = _statuses(calls)
	assert len(posts) == 1
	assert posts[0][:4] == ["api", "-X", "POST", f"repos/o/r/statuses/{HEAD}"]
	assert "state=success" in posts[0]
	assert "context=ai-review/head-gate" in posts[0]
	assert f"REVIEW_HEAD_GATE mode=updater_exempt pr=42 head={HEAD} outcome=posted reason=verified" in result.stdout
	assert [call[1] for call in calls if call[:2] != ["api", "-X"]] == ["repos/o/r/pulls/42", f"repos/o/r/commits/{HEAD}/status?per_page=100"]


def test_updater_exemption_reports_a_failed_post(tmp_path: Path) -> None:
	result, calls = _run_exempt(tmp_path, FAKE_FAIL="post")
	assert result.returncode == 1
	assert len(_statuses(calls)) == 1
	assert "outcome=skipped reason=post_failed" in result.stdout
	assert "outcome=posted reason=verified" not in result.stdout


def test_judge_updater_hold_rechecks_the_exemption() -> None:
	text = (ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	branch = text.index('echo "judge_action=updater_verify_hold" >> "$GITHUB_OUTPUT"')
	section = text[branch:text.index("exit 0", branch)]
	assert 'review_head_gate_updater_exempt "${REPOSITORY}" "${PR_NUMBER}" "${RB_JUDGED_HEAD_SHA:-}" "${POST_REVIEW_HEAD_REF}" || true' in section
	assert "gh pr merge" not in section


def test_review_tail_passes_its_pr_read_and_skips_the_pr_call(tmp_path: Path) -> None:
	result, calls = _run_exempt(tmp_path, pass_json=True)
	assert result.returncode == 0, result.stderr
	assert not any(call[:2] == ["api", "repos/o/r/pulls/42"] for call in calls)
	assert len(_statuses(calls)) == 1


@pytest.mark.parametrize("kwargs,reason", [
	({"status": _status(verify=None)}, "verify_missing"),
	({"status": _status(verify="failure")}, "verify_not_success"),
	({"status": _status(verify="pending")}, "verify_not_success"),
	({"status": _status(integrity=None)}, "integrity_missing"),
	({"status": _status(integrity="failure")}, "integrity_not_success"),
	({"status": _status(integrity=None, total=150)}, "status_page_incomplete"),
	({"status": {"statuses": "nope"}}, "invalid_response"),
	({"pr": _updater_pr(head={"ref": "ai/issue-1", "sha": HEAD, "repo": {"full_name": "o/r"}})}, "not_updater_branch"),
	({"pr": _updater_pr(head={"ref": UPDATER_REF, "sha": HEAD, "repo": None})}, "fork_head"),
	({"pr": _updater_pr(head={"ref": UPDATER_REF, "sha": HEAD, "repo": {"full_name": "evil/r"}})}, "fork_head"),
	({"pr": _updater_pr(head={"ref": UPDATER_REF, "sha": NEW_HEAD, "repo": {"full_name": "o/r"}})}, "head_moved"),
	({"pr": _updater_pr(state="closed")}, "pr_not_open"),
	({"pr": ["not", "an", "object"]}, "invalid_response"),
	({"FAKE_FAIL": "read"}, "pr_read_failed"),
	({"FAKE_FAIL": "status_read"}, "status_read_failed"),
])
def test_updater_exemption_fails_closed(tmp_path: Path, kwargs: dict, reason: str) -> None:
	result, calls = _run_exempt(tmp_path, **kwargs)
	assert result.returncode == 1
	assert not _statuses(calls)
	assert f"outcome=skipped reason={reason}" in result.stdout


@pytest.mark.parametrize("hint,env,reason", [
	("", {}, "head_ref_unavailable"),
	("auto/update-workflows-0123", {}, "not_updater_branch"),
	("ai/issue-9483", {}, "not_updater_branch"),
	(UPDATER_REF, {"REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED": "false"}, "disabled"),
	(UPDATER_REF, {"REVIEW_HEAD_GATE_STATUS_ENABLED": "false"}, "status_disabled"),
	(UPDATER_REF, {"REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED": "FALSE"}, "disabled"),
	(UPDATER_REF, {"REVIEW_HEAD_GATE_STATUS_ENABLED": "False"}, "status_disabled"),
])
def test_updater_exemption_prefilter_makes_no_api_call(tmp_path: Path, hint: str, env: dict, reason: str) -> None:
	result, calls = _run_exempt(tmp_path, hint=hint, **env)
	assert result.returncode == 1
	assert calls == []
	assert f"outcome=skipped reason={reason}" in result.stdout


def test_gate_main_runs_the_exemption_after_pending(tmp_path: Path) -> None:
	if shutil.which("jq") is None:
		pytest.skip("jq is required by the updater exemption")
	result, calls = _run(tmp_path, GATE_HEAD_REF="ai/issue-1", GATE_AUTO_MERGE_ENABLED="false")
	assert result.returncode == 0, result.stderr
	assert "mode=updater_exempt" in result.stdout and "reason=not_updater_branch" in result.stdout
	assert len(_statuses(calls)) == 1


def test_gate_main_without_a_head_ref_skips_the_exemption_silently(tmp_path: Path) -> None:
	# The early gate call passes no GATE_HEAD_REF: no updater_exempt log line.
	result, _calls = _run(tmp_path, GATE_HEAD_REF="", GATE_AUTO_MERGE_ENABLED="false")
	assert result.returncode == 0, result.stderr
	assert "mode=updater_exempt" not in result.stdout


def test_gate_main_skips_the_exemption_on_deterministic_skip(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, GATE_HEAD_REF=UPDATER_REF, DETERMINISTIC_SKIP="true", GATE_AUTO_MERGE_ENABLED="false")
	assert result.returncode == 0, result.stderr
	assert "mode=updater_exempt" not in result.stdout


def test_review_tail_calls_the_exemption_before_its_updater_exit() -> None:
	text = (ROOT / "scripts" / "review_enable_auto_merge.sh").read_text(encoding="utf-8")
	branch = text.index('if [[ "${_orch_pr_head_ref}" =~ ^auto/update-workflows-[0-9a-f]{12}$ ]]; then')
	section = text[branch:text.index("exit 0", branch)]
	assert 'review_head_gate_updater_exempt "${GITHUB_REPOSITORY}" "${PR_NUMBER}" "${INITIAL_HEAD_SHA:-}" "${_orch_pr_head_ref}" "${_ORCH_PR_META_JSON}" || true' in section
	assert "reason=updater_pr_verify_job_owns_merge" in section
	assert "gh pr merge" not in section


def test_review_autofix_passes_head_ref_and_switch_to_the_gate() -> None:
	text = (ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	step = text[text.index("        id: head_gate\n"):]
	step = step[:step.index("        run: |")]
	assert "GATE_HEAD_REF: ${{ steps.evaluate.outputs.head_ref }}" in step
	assert "REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED: ${{ vars.REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED || 'true' }}" in step
	tail = text[:text.index('bash "${SUPPORT_SCRIPTS_DIR}/review_enable_auto_merge.sh"')]
	tail = tail[tail.rindex("        env:\n"):]
	assert "REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED: ${{ vars.REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED || 'true' }}" in tail
	# The review-blocked judge re-checks the exemption, so it needs the switch too.
	judge = text[text.index("        id: rb_judge\n"):]
	judge = judge[:judge.index("        run: |")]
	assert "REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED: ${{ vars.REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED || 'true' }}" in judge
