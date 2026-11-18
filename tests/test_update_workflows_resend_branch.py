#!/usr/bin/env python3
"""Rewrite-refused resend of the updater's sync pull request.

A consumer ruleset with ``non_fast_forward`` on ``~ALL`` (fun-token-multi-chain
ruleset 14702897, run 44234623225: ``GH013 ... Cannot force-push to this
branch``) refuses the lease-checked refresh of an existing
``auto/update-workflows-<sha12>`` branch. The "Commit and push updates" step
then pushes the rebuilt sync to ``<branch>-r2`` (up to ``-r5``), opens the
sync pull request from it and closes the old one with a comment linking the
new one. Any other push failure keeps the fail-closed exit.

The cases drive the real step body (extracted from update_workflows.yml) with
the fixture of tests/test_update_workflows_pr_delivery.py: a bare file://
origin and a fake ``gh``. The refusal comes from the origin itself, either
``receive.denyNonFastForwards`` or a pre-receive hook that prints GitHub's
GH013 text.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_update_workflows_pr_delivery import (  # noqa: E402
	BRANCH,
	PAT_LOGIN,
	UPSTREAM_SHA,
	Fixture,
	_git,
	_remote_sha,
	_run_text,
	needs_jq,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
NEW_PR_URL = "https://github.com/octo/consumer/pull/77"
UPDATER_EMAIL = "github-actions[bot]@users.noreply.github.com"

GH013_HOOK = """#!/usr/bin/env bash
# Refuse any update of an existing branch that is not a fast-forward, the way
# a ruleset with non_fast_forward on ~ALL does; creating a branch is allowed.
zero=0000000000000000000000000000000000000000
status=0
while read -r old new ref; do
  [ "${old}" = "${zero}" ] && continue
  if ! git merge-base --is-ancestor "${old}" "${new}"; then
    echo "error: GH013: Repository rule violations found for ${ref}."
    echo "- Cannot force-push to this branch"
    status=1
  fi
done
exit "${status}"
"""

OTHER_HOOK = """#!/usr/bin/env bash
echo "error: some unrelated server-side check failed"
exit 1
"""

# GitHub prints GH013 for every ruleset violation; only the non_fast_forward
# rule adds "Cannot force-push to this branch".
GH013_OTHER_RULE_HOOK = """#!/usr/bin/env bash
echo "error: GH013: Repository rule violations found for refs/heads/x."
echo "- Commits must have verified signatures."
exit 1
"""

# Answers `gh pr view ... --json mergeable` (the refused path's re-read of an
# UNKNOWN value) and hands every other call to the fixture's fake gh.
GH_PR_VIEW_WRAPPER = """#!/usr/bin/env bash
if [ "$1 $2" = "pr view" ]; then
  printf '%s\\n' "$*" >> "${{FAKE_GH_LOG}}"
  echo '{mergeable}'
  exit 0
fi
exec '{real_gh}' "$@"
"""

GIT_WRAPPER = """#!/usr/bin/env bash
# Run the side effect once, right before the first `git push`.
if [ "$1" = "push" ] && [ ! -e '{marker}' ]; then
  : > '{marker}'
  {side_effect} >/dev/null 2>&1
fi
exec '{real_git}' "$@"
"""


def _install_hook(fx: Fixture, body: str) -> None:
	hook = fx.origin / "hooks" / "pre-receive"
	hook.write_text(body, encoding="utf-8")
	hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def _open_pr(number: int, branch: str, author: str = PAT_LOGIN, state: str = "OPEN", mergeable: str = "CONFLICTING") -> dict:
	return {
		"number": number,
		"url": f"https://github.com/octo/consumer/pull/{number}",
		"state": state,
		"isCrossRepository": False,
		"headRefName": branch,
		"mergedAt": None,
		"id": f"PR_node{number}",
		"autoMergeRequest": None,
		"author": {"login": author},
		"mergeable": mergeable,
	}


def _seed_updater_branch(fx: Fixture, branch: str) -> str:
	"""Put a divergent updater-looking commit on ``branch`` and record its push."""
	work = fx.tmp / f"seed-{branch.rsplit('-', 1)[-1]}-{len(list(fx.tmp.glob('seed-*')))}"
	_git(fx.tmp, "clone", "-q", fx.origin_url, str(work))
	(work / "stale.txt").write_text(branch, encoding="utf-8")
	_git(work, "add", "-A")
	message = f"old sync\n\nUpdater-Release-SHA: {UPSTREAM_SHA}"
	subprocess.run(
		["git", "-c", "user.name=bot", "-c", f"user.email={UPDATER_EMAIL}", "-c", "commit.gpgsign=false", "commit", "-q", "-m", message],
		cwd=work,
		check=True,
		capture_output=True,
	)
	_git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
	sha = _remote_sha(fx.origin, f"refs/heads/{branch}")
	fx.compare.write_text(json.dumps({"ahead_by": 1, "commits": [{"commit": {"committer": {"email": UPDATER_EMAIL}, "message": message}}]}), encoding="utf-8")
	fx.activity.write_text(json.dumps([{"ref": f"refs/heads/{branch}", "after": sha, "activity_type": "push", "actor": {"login": PAT_LOGIN}}]), encoding="utf-8")
	return sha


def _calls(fx: Fixture, prefix: str) -> list[str]:
	return [call for call in fx.gh_calls() if call.startswith(prefix)]


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
	return Fixture(tmp_path)


# ── Structural contract ──────────────────────────────────────────────────


def test_only_a_rewrite_refusal_selects_the_resend() -> None:
	run = _run_text()
	assert "UPDATER_RESEND_MAX=5" in run
	assert "grep -Eqi 'cannot force-push|protected branch hook declined|denying non-fast-forward|\\[remote rejected\\].*non-fast-forward'" in run
	refresh = run.index('git push --force-with-lease="refs/heads/${BRANCH}:${REMOTE_SHA}"')
	classify = run.index("grep -Eqi 'cannot force-push", refresh)
	fallback = run.index("pr_fail lease_mismatch_or_push_rejected", classify)
	resend_push = run.index('git push --force-with-lease="refs/heads/${BRANCH}:" origin', classify)
	assert refresh < classify < fallback < resend_push
	assert "pr_fail resend_cap_reached" in run
	assert "pr_fail resend_push_failed" in run
	# Never deletes a branch.
	assert "--delete" not in run and ":refs/heads/" not in run.replace('"HEAD:refs/heads/', "")


def test_review_guards_cover_resend_branches() -> None:
	resend_re = "^auto/update-workflows-[0-9a-f]{12}(-r[2-5])?$"
	paths = [
		".github/workflows/review_autofix.yml",
		"scripts/review_rb_judge.sh",
		"scripts/review_enable_auto_merge.sh",
		"scripts/review_autofix_step_detect_merge_conflicts.sh",
		"scripts/review_commit_changes.sh",
	]
	for rel in paths:
		text = (REPO_ROOT / rel).read_text(encoding="utf-8")
		assert resend_re in text, rel
		assert "update-workflows-[0-9a-f]{12}$" not in text, rel
	run = _run_text()
	assert 'test("^auto/update-workflows-[0-9a-f]{12}(-r[2-5])?$")' in run


@pytest.mark.parametrize(
	("ref", "covers"),
	[
		("auto/update-workflows-0123456789ab", True),
		("auto/update-workflows-0123456789ab-r2", True),
		("auto/update-workflows-0123456789ab-r5", True),
		("auto/update-workflows-aaaaaaaaaaaa-r3", False),
		("auto/update-workflows-aaaaaaaaaaaa", False),
		("ai-sync/stable", True),
	],
)
def test_drift_audit_reads_the_release_of_a_resend_branch(ref: str, covers: bool) -> None:
	sys.path.insert(0, str(REPO_ROOT / "scripts"))
	import audit_consumer_drift

	assert audit_consumer_drift._sync_pull_request_is_for_release(ref, UPSTREAM_SHA) is covers


# ── Behavioural tests ────────────────────────────────────────────────────


@pytest.mark.parametrize("refusal", ["deny_non_fast_forwards", "gh013_hook"])
@needs_jq
def test_rewrite_refusal_moves_the_sync_to_a_new_branch(fx: Fixture, refusal: str) -> None:
	old_sha = _seed_updater_branch(fx, BRANCH)
	if refusal == "deny_non_fast_forwards":
		_git(fx.origin, "config", "receive.denyNonFastForwards", "true")
	else:
		_install_hook(fx, GH013_HOOK)
	fx.pr_list.write_text(json.dumps([_open_pr(5, BRANCH)]), encoding="utf-8")
	fx.stale_list.write_text(json.dumps([
		{"number": 5, "url": "u5", "headRefName": BRANCH, "isCrossRepository": False, "author": {"login": PAT_LOGIN}},
		{"number": 11, "url": "u11", "headRefName": "auto/update-workflows-aaaaaaaaaaaa-r2", "isCrossRepository": False, "author": {"login": PAT_LOGIN}},
	]), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stdout + result.stderr
	new_branch = f"{BRANCH}-r2"
	# The old branch is untouched and not deleted; the sync is on -r2.
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == old_sha
	pushed = _remote_sha(fx.origin, f"refs/heads/{new_branch}")
	assert pushed and pushed != old_sha
	assert _remote_sha(fx.origin, "refs/heads/main") == fx.base_sha
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r3") == ""
	creates = _calls(fx, "pr create")
	assert len(creates) == 1 and f"--head {new_branch}" in creates[0]
	assert "chore: update AI workflow wrappers from coding-workflows@stable (v1.2.3)" in creates[0]
	body = fx.body_copy.read_text(encoding="utf-8")
	assert f"<!-- ai:update-workflows-pr:v1 release_sha={UPSTREAM_SHA} -->" in body
	# The old PR is closed once, with a comment linking the new one; the stale
	# sweep closes the older release's resend PR and skips #5.
	closes_5 = _calls(fx, "pr close 5 ")
	assert len(closes_5) == 1
	assert NEW_PR_URL in closes_5[0] and new_branch in closes_5[0]
	assert len(_calls(fx, "pr close 11 ")) == 1
	outputs = fx.outputs()
	assert outputs["branch"] == new_branch
	assert outputs["pr_action"] == "created"
	assert outputs["pr_number"] == "77"
	assert outputs["pushed_sha"] == pushed
	assert outputs["auto_merge"] == "pending_verify"
	assert f"UPDATER_PR_RESEND outcome=pushed from={BRANCH} branch={new_branch} old_pr=5" in result.stdout
	assert f"UPDATER_PR_RESEND outcome=opened from={BRANCH} branch={new_branch} pr=77 old_pr=5 old_pr_closed=true" in result.stdout
	assert "UPDATER_PR_DELIVERY outcome=opened reason=resent_rewrite_refused" in result.stdout


@needs_jq
def test_lease_mismatch_keeps_todays_failure(fx: Fixture) -> None:
	old_sha = _seed_updater_branch(fx, BRANCH)
	_git(fx.origin, "config", "receive.denyNonFastForwards", "true")
	fx.pr_list.write_text(json.dumps([_open_pr(5, BRANCH)]), encoding="utf-8")
	# Someone else moves the branch (fast-forward) between the script's fetch
	# and its push: the lease no longer matches, so git refuses client-side.
	mover = fx.tmp / "mover"
	_git(fx.tmp, "clone", "-q", "-b", BRANCH, fx.origin_url, str(mover))
	(mover / "moved.txt").write_text("moved\n", encoding="utf-8")
	_git(mover, "add", "-A")
	_git(mover, "commit", "-q", "-m", "moved")
	moved_sha = _git(mover, "rev-parse", "HEAD")
	real_git = shutil.which("git")
	assert real_git
	wrapper = fx.bin / "git"
	marker = fx.tmp / "git_push_marker"
	side_effect = f"'{real_git}' -C '{mover}' push -q origin HEAD:refs/heads/{BRANCH}"
	wrapper.write_text(GIT_WRAPPER.format(marker=marker, side_effect=side_effect, real_git=real_git), encoding="utf-8")
	wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
	result = fx.run()
	assert result.returncode != 0
	assert "reason=lease_mismatch_or_push_rejected" in result.stdout
	assert marker.exists()
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == moved_sha != old_sha
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r2") == ""
	assert "UPDATER_PR_RESEND" not in result.stdout
	assert not _calls(fx, "pr create") and not _calls(fx, "pr close")


@needs_jq
def test_other_remote_rejection_keeps_todays_failure(fx: Fixture) -> None:
	old_sha = _seed_updater_branch(fx, BRANCH)
	_install_hook(fx, OTHER_HOOK)
	fx.pr_list.write_text(json.dumps([_open_pr(5, BRANCH)]), encoding="utf-8")
	result = fx.run()
	assert result.returncode != 0
	assert "reason=lease_mismatch_or_push_rejected" in result.stdout
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == old_sha
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r2") == ""
	assert "UPDATER_PR_RESEND" not in result.stdout
	assert not _calls(fx, "pr create") and not _calls(fx, "pr close")


@needs_jq
def test_later_run_uses_the_highest_resend_branch(fx: Fixture) -> None:
	# Run after a resend: the base branch's PR is closed (by the updater), -r2
	# carries the open PR, and an out-of-range -r6 is ignored. A refusal on -r2
	# moves the sync to -r3, the first free name above it.
	_git(fx.origin, "update-ref", f"refs/heads/{BRANCH}", fx.base_sha)
	_git(fx.origin, "update-ref", f"refs/heads/{BRANCH}-r6", fx.base_sha)
	old_sha = _seed_updater_branch(fx, f"{BRANCH}-r2")
	_install_hook(fx, GH013_HOOK)
	fx.pr_list.write_text(json.dumps([_open_pr(4, BRANCH, state="CLOSED"), _open_pr(6, f"{BRANCH}-r2")]), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stdout + result.stderr
	lookup = [call for call in _calls(fx, "pr list") if "--head" in call]
	assert len(lookup) == 1 and f"--head {BRANCH}-r2 " in lookup[0]
	assert "closed_by_human" not in result.stdout
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r2") == old_sha
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r3") != ""
	assert len(_calls(fx, "pr close 6 ")) == 1
	assert not _calls(fx, "pr close 4 ")
	assert fx.outputs()["branch"] == f"{BRANCH}-r3"


@needs_jq
def test_refresh_of_a_resend_branch_without_refusal_stays_on_it(fx: Fixture) -> None:
	_seed_updater_branch(fx, f"{BRANCH}-r2")
	fx.pr_list.write_text(json.dumps([_open_pr(4, BRANCH, state="CLOSED"), _open_pr(6, f"{BRANCH}-r2")]), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stdout + result.stderr
	outputs = fx.outputs()
	assert outputs["branch"] == f"{BRANCH}-r2"
	assert outputs["pr_action"] == "refreshed"
	assert outputs["pr_number"] == "6"
	assert outputs["pushed_sha"] == _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r2")
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r3") == ""
	assert "UPDATER_PR_RESEND" not in result.stdout
	assert not _calls(fx, "pr create") and not _calls(fx, "pr close")


@needs_jq
def test_resend_cap_stops_at_r5(fx: Fixture) -> None:
	for suffix in ("", "-r2", "-r3", "-r4"):
		_git(fx.origin, "update-ref", f"refs/heads/{BRANCH}{suffix}", fx.base_sha)
	old_sha = _seed_updater_branch(fx, f"{BRANCH}-r5")
	_install_hook(fx, GH013_HOOK)
	fx.pr_list.write_text(json.dumps([_open_pr(9, f"{BRANCH}-r5")]), encoding="utf-8")
	result = fx.run()
	assert result.returncode != 0
	assert f"UPDATER_PR_RESEND outcome=cap_reached from={BRANCH}-r5 max=-r5 old_pr=9" in result.stdout
	assert "UPDATER_PR_DELIVERY outcome=failed reason=resend_cap_reached" in result.stdout
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r5") == old_sha
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r6") == ""
	assert not _calls(fx, "pr create") and not _calls(fx, "pr close")


@needs_jq
def test_old_pr_by_another_author_is_left_open(fx: Fixture) -> None:
	_seed_updater_branch(fx, BRANCH)
	_install_hook(fx, GH013_HOOK)
	fx.pr_list.write_text(json.dumps([_open_pr(5, BRANCH, author="human")]), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stdout + result.stderr
	assert not _calls(fx, "pr close 5 ")
	assert "old_pr_closed=not_pat_author" in result.stdout
	assert fx.outputs()["branch"] == f"{BRANCH}-r2"


@needs_jq
def test_failed_resend_push_creates_nothing(fx: Fixture) -> None:
	# The -r2 name is taken between the name pick and the push (the hook
	# refuses creating it): the run fails, and never tries a second name.
	_seed_updater_branch(fx, BRANCH)
	hook = GH013_HOOK.replace('  [ "${old}" = "${zero}" ] && continue', '  if [ "${old}" = "${zero}" ]; then echo "error: create refused"; status=1; continue; fi')
	_install_hook(fx, hook)
	fx.pr_list.write_text(json.dumps([_open_pr(5, BRANCH)]), encoding="utf-8")
	result = fx.run()
	assert result.returncode != 0
	assert f"UPDATER_PR_RESEND outcome=push_failed from={BRANCH} branch={BRANCH}-r2 old_pr=5" in result.stdout
	assert "reason=resend_push_failed" in result.stdout
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r2") == ""
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r3") == ""
	assert not _calls(fx, "pr create") and not _calls(fx, "pr close")


@pytest.mark.parametrize("mergeable", ["MERGEABLE", "UNKNOWN", None])
@needs_jq
def test_refused_refresh_of_a_mergeable_pr_keeps_its_head(fx: Fixture, mergeable: str | None) -> None:
	# Under a no-force-push rule every refresh is refused, because the commit is
	# rebuilt on the current default branch. A PR that is not conflicting keeps
	# its head (same release) and the verify job re-checks it: no new branch.
	old_sha = _seed_updater_branch(fx, BRANCH)
	_install_hook(fx, GH013_HOOK)
	pr = _open_pr(5, BRANCH, mergeable=mergeable or "")
	if mergeable is None:
		pr.pop("mergeable")
	fx.pr_list.write_text(json.dumps([pr]), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stdout + result.stderr
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == old_sha
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r2") == ""
	outputs = fx.outputs()
	assert outputs["branch"] == BRANCH
	assert outputs["pr_number"] == "5"
	assert outputs["pr_action"] == "refreshed"
	assert outputs["pushed_sha"] == old_sha
	assert f"UPDATER_PR_RESEND outcome=kept_head branch={BRANCH} pr=5 mergeable={mergeable or 'unknown'}" in result.stdout
	assert "UPDATER_PR_DELIVERY outcome=refreshed reason=rewrite_refused_kept_head" in result.stdout
	assert not _calls(fx, "pr create") and not _calls(fx, "pr close")


@needs_jq
def test_refused_refresh_without_an_open_pr_resends(fx: Fixture) -> None:
	# A branch left without a PR (an earlier create failed) has nothing to keep.
	_seed_updater_branch(fx, BRANCH)
	_install_hook(fx, GH013_HOOK)
	result = fx.run()
	assert result.returncode == 0, result.stdout + result.stderr
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r2") != ""
	assert f"UPDATER_PR_RESEND outcome=opened from={BRANCH} branch={BRANCH}-r2 pr=77 old_pr=none old_pr_closed=none" in result.stdout
	assert not _calls(fx, "pr close")


@needs_jq
def test_other_ruleset_violation_is_not_a_rewrite_refusal(fx: Fixture) -> None:
	# A GH013 refusal for another rule (signatures here) must not keep the head
	# as if the refresh had landed, nor resend to a branch that rule refuses too.
	old_sha = _seed_updater_branch(fx, BRANCH)
	_install_hook(fx, GH013_OTHER_RULE_HOOK)
	fx.pr_list.write_text(json.dumps([_open_pr(5, BRANCH, mergeable="MERGEABLE")]), encoding="utf-8")
	result = fx.run()
	assert result.returncode != 0
	assert "reason=lease_mismatch_or_push_rejected" in result.stdout
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == old_sha
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}-r2") == ""
	assert "UPDATER_PR_RESEND" not in result.stdout
	assert not _calls(fx, "pr create") and not _calls(fx, "pr close")


@pytest.mark.parametrize(("reread", "resent"), [("CONFLICTING", True), ("MERGEABLE", False)])
@needs_jq
def test_unknown_mergeable_is_read_once_more(fx: Fixture, reread: str, resent: bool) -> None:
	_seed_updater_branch(fx, BRANCH)
	_install_hook(fx, GH013_HOOK)
	fx.pr_list.write_text(json.dumps([_open_pr(5, BRANCH, mergeable="UNKNOWN")]), encoding="utf-8")
	gh = fx.bin / "gh"
	real_gh = fx.bin / "gh-fixture"
	gh.rename(real_gh)
	gh.write_text(GH_PR_VIEW_WRAPPER.format(mergeable=reread, real_gh=real_gh), encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
	result = fx.run()
	assert result.returncode == 0, result.stdout + result.stderr
	views = _calls(fx, "pr view")
	assert len(views) == 1 and views[0].startswith("pr view 5 ") and "--json mergeable" in views[0]
	if resent:
		assert fx.outputs()["branch"] == f"{BRANCH}-r2"
		assert len(_calls(fx, "pr close 5 ")) == 1
	else:
		assert fx.outputs()["branch"] == BRANCH
		assert f"UPDATER_PR_RESEND outcome=kept_head branch={BRANCH} pr=5 mergeable=MERGEABLE" in result.stdout
		assert not _calls(fx, "pr create") and not _calls(fx, "pr close")
