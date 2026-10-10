#!/usr/bin/env python3
"""Contract tests for the updater's pull-request delivery (issue #6989).

With UPDATER_PR_DELIVERY_ENABLED=true the "Commit and push updates" step of
update_workflows.yml must never push to the consumer default branch. It
commits to auto/update-workflows-<release-sha-12> with a lease-checked push,
opens or refreshes one pull request found by a head+base scoped lookup, runs
the auto-close lint before creating it, and enables auto-merge (bound to the
pushed head) only when manifest verification reported verified=true; since
#7004 the separate `verify` job enables it after checking the PR tree, and
this step records auto_merge=pending_verify. With the flag off the step keeps
today's direct push.

The behavioural tests run the step body with bash against a bare file://
origin and a fake `gh` under tmp_path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
BUILDER = REPO_ROOT / "scripts" / "release_manifest.py"
LINT = REPO_ROOT / "scripts" / "lint_pr_body_auto_close.py"
STEP_NAME = "Commit and push updates"
STEP_ID = "commit_push"
UPSTREAM_SHA = "0123456789abcdef0123456789abcdef01234567"
BRANCH = "auto/update-workflows-0123456789ab"
BRANCH_RE = re.compile(r"^auto/update-workflows-[0-9a-f]{12}$")
REPO = "octo/consumer"
PAT_LOGIN = "pipeline-bot"
EXPECTED_PREDICATE = (
	"steps.update.outputs.has_updates == 'true' || steps.audit_gate.outputs.status == 'applied' || "
	"steps.claude_sync.outputs.claude_has_changes == 'true' || steps.retired_files.outputs.retired_has_changes == 'true' || "
	"steps.claude_md_sync.outputs.claude_md_changed == 'true' || steps.changelog_sync.outputs.changelog_assets_has_changes == 'true' || "
	"steps.changelog_assemble.outputs.changelog_assembled == 'true'"
)
AUTO_CLOSE_RE = re.compile(r"(?i)(?<![\w-])(close[sd]?|fix(e[sd])?|resolve[sd]?)\s+(?:[\w.-]+/[\w.-]+)?#\d+")


def _doc() -> dict:
	return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict:
	return _doc()["jobs"]["update-wrappers"]


def _steps() -> list[dict]:
	return _job()["steps"]


def _step(name: str = STEP_NAME) -> dict:
	matches = [step for step in _steps() if step.get("name") == name]
	assert len(matches) == 1, name
	return matches[0]


def _run_text() -> str:
	return _step()["run"]


# ── Structural contract ──────────────────────────────────────────────────


def test_step_name_predicate_and_id() -> None:
	step = _step()
	assert step["id"] == STEP_ID
	assert step["if"] == EXPECTED_PREDICATE
	assert "continue-on-error" not in step


def test_flag_defaults_off_in_workflow_and_shell() -> None:
	step = _step()
	env = step["env"]
	assert env["UPDATER_PR_DELIVERY_ENABLED"] == "${{ vars.UPDATER_PR_DELIVERY_ENABLED || 'false' }}"
	assert env["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert env["VERIFIED"] == "${{ steps.verify_release_manifest.outputs.verified }}"
	assert env["UPSTREAM_SHA"] == "${{ steps.fetch.outputs.upstream_sha }}"
	assert '"${UPDATER_PR_DELIVERY_ENABLED:-false}"' in step["run"]


def test_branch_pattern_never_matches_merge_train_or_forward_merge() -> None:
	run = _run_text()
	assert 'BRANCH="auto/update-workflows-${UPSTREAM_SHA:0:12}"' in run
	assert "^auto/update-workflows-[0-9a-f]{12}$" in run
	assert BRANCH_RE.match(BRANCH)
	for name in ("ai/issue-6989", "auto/forward-merge-stable-abc", "auto/update-workflows-0123", "main"):
		assert not BRANCH_RE.match(name), name


def test_bare_push_only_on_the_flag_off_path() -> None:
	run = _run_text()
	lines = [line.strip() for line in run.splitlines()]
	bare = [index for index, line in enumerate(lines) if line == "git push"]
	assert len(bare) == 1
	gate = lines.index('if [ "${PR_DELIVERY}" != "true" ]; then')
	assert gate < bare[0] < gate + 5
	assert lines[bare[0] + 1] == "exit 0"
	pushes = [line for line in lines if "git push" in line and line != "git push"]
	assert len(pushes) == 2
	for line in pushes:
		assert '--force-with-lease="refs/heads/${BRANCH}:' in line
		assert 'origin "HEAD:refs/heads/${BRANCH}"' in line
		assert "BASE" not in line
	assert '--force-with-lease="refs/heads/${BRANCH}:"' in run
	assert '--force-with-lease="refs/heads/${BRANCH}:${REMOTE_SHA}"' in run


def test_pr_lookup_is_scoped_to_head_and_base() -> None:
	run = _run_text()
	assert 'gh pr list --repo "${GITHUB_REPOSITORY}" --head "${BRANCH}" --base "${BASE}" --state all' in run
	assert "isCrossRepository == false" in run


def test_lint_runs_before_any_push_or_pr_write() -> None:
	run = _run_text()
	assert 'LINT_SCRIPT="${UPSTREAM_ROOT}/scripts/lint_pr_body_auto_close.py"' in run
	lint = run.index('"${LINT_SCRIPT}" --pr-body-file')
	assert lint < run.index('--force-with-lease="refs/heads/${BRANCH}:"')
	assert lint < run.index("gh pr create")
	assert lint < run.index("gh pr edit")
	assert "--fail-open-on-lookup-error" not in run
	assert "pr_fail pr_body_lint_failed" in run


def test_auto_merge_is_bound_to_head_and_gated_on_verification() -> None:
	# Since #7004 this step never enables auto-merge; the `verify` job does,
	# after its PR-tree check (tests/test_update_workflows_template_gate.py).
	run = _run_text()
	gate = run.index('if [ "${VERIFIED_TEXT}" = "true" ]; then')
	pending = run.index('AUTO_MERGE="pending_verify"')
	assert gate < pending < run.index("AUTO_MERGE=\"skipped_unverified\"")
	assert "gh pr merge" not in run
	verify_steps = _doc()["jobs"]["verify"]["steps"]
	publish = [step for step in verify_steps if step.get("id") == "verify_publish"]
	assert len(publish) == 1
	assert '--auto --squash --match-head-commit "${PUSHED_SHA}"' in publish[0]["run"]


def test_no_pull_requests_permission_is_requested() -> None:
	doc = _doc()
	assert "pull-requests" not in json.dumps(doc.get("permissions", {}))
	assert "pull-requests" not in json.dumps(_job().get("permissions", {}))
	for step in _steps():
		run = step.get("run", "")
		if "gh pr " in run or "gh api" in run:
			assert step.get("env", {}).get("GH_TOKEN") == "${{ secrets.GH_PAT }}", step.get("name")


def test_notification_and_summary_report_the_pull_request() -> None:
	telegram = _step("Send Telegram notification")
	assert telegram["env"]["COMMIT_PR_URL"] == "${{ steps.commit_push.outputs.pr_url }}"
	assert '"${TG_LEVEL}"' in telegram["run"]
	summary = _step("Summary")
	assert summary["if"] == "always()"
	assert summary["env"]["COMMIT_PR_URL"] == "${{ steps.commit_push.outputs.pr_url }}"
	assert "- **Pull request:**" in summary["run"]
	assert "- **Auto-merge:**" in summary["run"]


def test_lint_script_is_attested() -> None:
	verify = _step("Verify attested release manifest")["run"]
	assert "scripts/lint_pr_body_auto_close.py" in verify
	assert '("file", "scripts/lint_pr_body_auto_close.py", True)' in BUILDER.read_text(encoding="utf-8")


# ── Behavioural tests ────────────────────────────────────────────────────


def _clean_env() -> dict[str, str]:
	return {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITHUB_", "UPDATER_", "UPSTREAM_", "FAKE_"))}


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(
		["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
		cwd=cwd,
		check=True,
		capture_output=True,
		text=True,
		env=_clean_env(),
	).stdout.strip()


def _remote_sha(origin: Path, ref: str) -> str:
	out = _git(origin, "ls-remote", str(origin), ref)
	return out.split()[0] if out else ""


FAKE_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
body_file=""
prev=""
for arg in "$@"; do
  if [ "${prev}" = "--body-file" ]; then body_file="${arg}"; fi
  prev="${arg}"
done
case "$1 $2" in
  "pr list")
    case "$*" in
      *--search*) cat "${FAKE_STALE_LIST}" ;;
      *) cat "${FAKE_PR_LIST}" ;;
    esac
    exit 0 ;;
  "pr create")
    cp "${body_file}" "${FAKE_BODY_COPY}"
    echo "https://github.com/octo/consumer/pull/77"
    exit 0 ;;
  "pr edit")
    cp "${body_file}" "${FAKE_BODY_COPY}"
    exit 0 ;;
  "pr close"|"pr merge") exit 0 ;;
  "issue view") echo '[]'; exit 0 ;;
esac
if [ "$1" = "api" ]; then
  case "$2" in
    user) echo "${FAKE_PAT_LOGIN}"; exit 0 ;;
    graphql) exit "${FAKE_GRAPHQL_RC:-0}" ;;
    */activity*) cat "${FAKE_ACTIVITY_JSON}"; exit 0 ;;
    */compare/*)
      if [ -n "${FAKE_COMPARE_SIDE_EFFECT:-}" ]; then bash -c "${FAKE_COMPARE_SIDE_EFFECT}" >/dev/null 2>&1; fi
      cat "${FAKE_COMPARE_JSON}"; exit 0 ;;
  esac
fi
exit 1
"""


class Fixture:
	def __init__(self, tmp_path: Path):
		self.tmp = tmp_path
		self.origin = tmp_path / "origin.git"
		self.consumer = tmp_path / "consumer"
		self.upstream = tmp_path / "upstream"
		self.bin = tmp_path / "bin"
		self.state = tmp_path / "state"
		self.state.mkdir()
		seed = tmp_path / "seed"
		seed.mkdir()
		_git(seed, "init", "-q", "-b", "main")
		(seed / ".github" / "workflows").mkdir(parents=True)
		(seed / ".github" / "workflows" / "ai-review.yml").write_text("name: old\n", encoding="utf-8")
		_git(seed, "add", "-A")
		_git(seed, "commit", "-q", "-m", "base")
		_git(tmp_path, "clone", "-q", "--bare", str(seed), str(self.origin))
		self.origin_url = f"file://{self.origin}"
		_git(tmp_path, "clone", "-q", "--depth", "1", "-b", "main", self.origin_url, str(self.consumer))
		self.base_sha = _remote_sha(self.origin, "refs/heads/main")
		(self.upstream / "workflow-templates").mkdir(parents=True)
		(self.upstream / "scripts").mkdir()
		shutil.copy2(LINT, self.upstream / "scripts" / LINT.name)
		self.bin.mkdir()
		gh = self.bin / "gh"
		gh.write_text(FAKE_GH, encoding="utf-8")
		gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
		self.pr_list = self.tmp / "pr_list.json"
		self.stale_list = self.tmp / "stale_list.json"
		self.compare = self.tmp / "compare.json"
		self.activity = self.tmp / "activity.json"
		self.body_copy = self.tmp / "body_copy.md"
		self.gh_log = self.tmp / "gh.log"
		self.output = self.tmp / "github_output"
		for path in (self.pr_list, self.stale_list, self.activity):
			path.write_text("[]", encoding="utf-8")
		self.compare.write_text(json.dumps({"ahead_by": 0, "commits": []}), encoding="utf-8")

	def stage_change(self) -> None:
		(self.consumer / ".github" / "workflows" / "ai-review.yml").write_text("name: new\n", encoding="utf-8")
		(self.state / "updated_files.txt").write_text("• ai-review.yml\n", encoding="utf-8")
		(self.state / "created_files.txt").write_text("", encoding="utf-8")

	def run(self, flag: str = "true", verified: str = "true", side_effect: str = "", release_tag: str = "v1.2.3", upstream_sha: str = UPSTREAM_SHA) -> subprocess.CompletedProcess:
		self.stage_change()
		self.output.write_text("", encoding="utf-8")
		script = _run_text().replace("/tmp/", f"{self.state}/")
		script = script.replace("${{ steps.changelog_assemble.outputs.changelog_assembled }}", "false")
		script = script.replace("${{ steps.changelog_assemble.outputs.changelog_fragment_count }}", "0")
		script = script.replace("${{ steps.changelog_assemble.outputs.changelog_layout }}", "none")
		assert "${{" not in script
		env = {
			**_clean_env(),
			"HOME": str(self.tmp),
			"GIT_CONFIG_NOSYSTEM": "1",
			"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
			"GITHUB_OUTPUT": str(self.output),
			"GITHUB_REPOSITORY": REPO,
			"GH_TOKEN": "dummy",
			"UPSTREAM_DIR": str(self.upstream / "workflow-templates"),
			"UPSTREAM_SHA": upstream_sha,
			"VERIFIED": verified,
			"RELEASE_TAG": release_tag,
			"DEFAULT_BRANCH": "main",
			"UPDATER_PR_DELIVERY_ENABLED": flag,
			"FAKE_GH_LOG": str(self.gh_log),
			"FAKE_PR_LIST": str(self.pr_list),
			"FAKE_STALE_LIST": str(self.stale_list),
			"FAKE_COMPARE_JSON": str(self.compare),
			"FAKE_ACTIVITY_JSON": str(self.activity),
			"FAKE_COMPARE_SIDE_EFFECT": side_effect,
			"FAKE_BODY_COPY": str(self.body_copy),
			"FAKE_PAT_LOGIN": PAT_LOGIN,
			"TMPDIR": str(self.tmp),
		}
		return subprocess.run(["bash", "-c", script], cwd=self.consumer, env=env, capture_output=True, text=True)

	def gh_calls(self) -> list[str]:
		if not self.gh_log.exists():
			return []
		return self.gh_log.read_text(encoding="utf-8").splitlines()

	def outputs(self) -> dict[str, str]:
		return dict(line.partition("=")[::2] for line in self.output.read_text(encoding="utf-8").splitlines() if "=" in line)

	def seed_branch(self, author_email: str, message: str) -> str:
		work = self.tmp / f"squatter-{len(list(self.tmp.glob('squatter-*')))}"
		_git(self.tmp, "clone", "-q", self.origin_url, str(work))
		(work / "evil.txt").write_text(message, encoding="utf-8")
		_git(work, "add", "-A")
		subprocess.run(
			["git", "-c", "user.name=someone", "-c", f"user.email={author_email}", "-c", "commit.gpgsign=false", "commit", "-q", "-m", message],
			cwd=work,
			check=True,
			capture_output=True,
			env=_clean_env(),
		)
		_git(work, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
		return _remote_sha(self.origin, f"refs/heads/{BRANCH}")

	def record_push(self, after: str, login: str = PAT_LOGIN) -> None:
		self.activity.write_text(json.dumps([{"ref": f"refs/heads/{BRANCH}", "after": after, "activity_type": "push", "actor": {"login": login}}]), encoding="utf-8")


needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the step body")


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
	return Fixture(tmp_path)


@needs_jq
def test_flag_off_keeps_the_direct_push(fx: Fixture) -> None:
	result = fx.run(flag="false")
	assert result.returncode == 0, result.stderr
	assert _remote_sha(fx.origin, "refs/heads/main") != fx.base_sha
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == ""
	assert not any(call.startswith("pr ") for call in fx.gh_calls())
	assert fx.outputs()["delivery"] == "direct"
	message = _git(fx.origin, "log", "-1", "--format=%B", "main")
	assert "Updater-Release-SHA" not in message


@needs_jq
def test_fresh_run_opens_one_pr_without_touching_main(fx: Fixture) -> None:
	result = fx.run()
	assert result.returncode == 0, result.stdout + result.stderr
	assert _remote_sha(fx.origin, "refs/heads/main") == fx.base_sha
	pushed = _remote_sha(fx.origin, f"refs/heads/{BRANCH}")
	assert pushed
	calls = fx.gh_calls()
	assert sum(call.startswith("pr create") for call in calls) == 1
	assert not any(call.startswith("pr edit") for call in calls)
	lookup = [call for call in calls if call.startswith("pr list") and "--head" in call]
	assert len(lookup) == 1 and f"--head {BRANCH}" in lookup[0] and "--base main" in lookup[0]
	body = fx.body_copy.read_text(encoding="utf-8")
	assert not AUTO_CLOSE_RE.search(body)
	assert UPSTREAM_SHA in body and "v1.2.3" in body
	assert "`.github/workflows/ai-review.yml`" in body
	assert f"<!-- ai:update-workflows-pr:v1 release_sha={UPSTREAM_SHA} -->" in body
	message = _git(fx.origin, "log", "-1", "--format=%B", BRANCH)
	assert f"Updater-Release-SHA: {UPSTREAM_SHA}" in message.splitlines()
	outputs = fx.outputs()
	assert outputs["delivery"] == "pr"
	assert outputs["pr_action"] == "created"
	assert outputs["pr_url"] == "https://github.com/octo/consumer/pull/77"
	assert outputs["pushed_sha"] == pushed
	assert "UPDATER_PR_DELIVERY outcome=opened" in result.stdout


@needs_jq
def test_verified_release_waits_for_the_verify_job(fx: Fixture) -> None:
	result = fx.run(verified="true")
	assert result.returncode == 0, result.stderr
	pushed = _remote_sha(fx.origin, f"refs/heads/{BRANCH}")
	assert not any(call.startswith("pr merge") for call in fx.gh_calls())
	outputs = fx.outputs()
	assert outputs["auto_merge"] == "pending_verify"
	assert outputs["pushed_sha"] == pushed
	assert outputs["base"] == "main"
	assert "ai-update-workflows/verify" in fx.body_copy.read_text(encoding="utf-8")


@pytest.mark.parametrize("verified", ["skipped", "false", ""])
@needs_jq
def test_unverified_release_is_left_for_a_human(fx: Fixture, verified: str) -> None:
	result = fx.run(verified=verified)
	assert result.returncode == 0, result.stderr
	assert not any(call.startswith("pr merge") for call in fx.gh_calls())
	assert fx.outputs()["auto_merge"] == "skipped_unverified"
	assert "::warning::" in result.stdout


@needs_jq
def test_rerun_with_open_pr_refreshes_it(fx: Fixture) -> None:
	fx.pr_list.write_text(json.dumps([{"number": 5, "url": "https://github.com/octo/consumer/pull/5", "state": "OPEN", "isCrossRepository": False, "headRefName": BRANCH, "mergedAt": None}]), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stderr
	calls = fx.gh_calls()
	assert any(call.startswith(f"pr edit 5 --repo {REPO}") for call in calls)
	assert not any(call.startswith("pr create") for call in calls)
	assert fx.outputs()["pr_action"] == "refreshed"
	assert _remote_sha(fx.origin, "refs/heads/main") == fx.base_sha


AUTO_ENROLLED_PR = [{"number": 5, "url": "https://github.com/octo/consumer/pull/5", "state": "OPEN", "isCrossRepository": False, "headRefName": BRANCH, "mergedAt": None, "id": "PR_node5", "autoMergeRequest": {"mergeMethod": "SQUASH"}}]


@needs_jq
def test_refresh_withdraws_earlier_auto_merge_before_push(fx: Fixture) -> None:
	fx.pr_list.write_text(json.dumps(AUTO_ENROLLED_PR), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stderr
	calls = fx.gh_calls()
	withdraw = [i for i, call in enumerate(calls) if call.startswith("api graphql") and "disablePullRequestAutoMerge" in call]
	assert len(withdraw) == 1
	assert "id=PR_node5" in calls[withdraw[0]]
	edit = next(i for i, call in enumerate(calls) if call.startswith("pr edit 5"))
	assert withdraw[0] < edit
	assert fx.outputs()["auto_merge"] == "pending_verify"


@needs_jq
def test_refresh_without_enrollment_skips_withdrawal(fx: Fixture) -> None:
	fx.pr_list.write_text(json.dumps([{**AUTO_ENROLLED_PR[0], "autoMergeRequest": None}]), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stderr
	assert not any(call.startswith("api graphql") for call in fx.gh_calls())


@needs_jq
def test_failed_auto_merge_withdrawal_fails_closed(fx: Fixture) -> None:
	fx.pr_list.write_text(json.dumps(AUTO_ENROLLED_PR), encoding="utf-8")
	(fx.bin / "gh").write_text(FAKE_GH.replace('graphql) exit "${FAKE_GRAPHQL_RC:-0}"', "graphql) exit 1"), encoding="utf-8")
	result = fx.run()
	assert result.returncode != 0
	assert "auto_merge_withdraw_failed" in result.stdout + result.stderr
	assert not any(call.startswith("pr edit") for call in fx.gh_calls())
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == ""


@needs_jq
def test_fork_pr_with_same_branch_name_is_ignored(fx: Fixture) -> None:
	fx.pr_list.write_text(json.dumps([{"number": 9, "url": "u", "state": "OPEN", "isCrossRepository": True, "headRefName": BRANCH, "mergedAt": None}]), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stderr
	assert not any(call.startswith("pr edit 9") for call in fx.gh_calls())
	assert fx.outputs()["pr_action"] == "created"


@needs_jq
def test_foreign_commit_on_branch_fails_closed(fx: Fixture) -> None:
	foreign = fx.seed_branch("someone@example.invalid", "sneaky change")
	fx.compare.write_text(json.dumps({"ahead_by": 1, "commits": [{"commit": {"committer": {"email": "someone@example.invalid"}, "message": "sneaky change"}}]}), encoding="utf-8")
	result = fx.run()
	assert result.returncode != 0
	assert "reason=foreign_commits_on_branch" in result.stdout
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == foreign
	assert _remote_sha(fx.origin, "refs/heads/main") == fx.base_sha
	assert not any(call.startswith(("pr create", "pr edit", "pr merge")) for call in fx.gh_calls())


@needs_jq
def test_branch_moved_during_run_fails_closed(fx: Fixture) -> None:
	_git(fx.origin, "update-ref", f"refs/heads/{BRANCH}", fx.base_sha)
	mover = fx.tmp / "mover"
	_git(fx.tmp, "clone", "-q", fx.origin_url, str(mover))
	(mover / "moved.txt").write_text("moved\n", encoding="utf-8")
	_git(mover, "add", "-A")
	_git(mover, "commit", "-q", "-m", "moved")
	moved_sha = _git(mover, "rev-parse", "HEAD")
	fx.record_push(fx.base_sha)
	side_effect = f"git -C '{mover}' push -q -f origin HEAD:refs/heads/{BRANCH}"
	result = fx.run(side_effect=side_effect)
	assert result.returncode != 0
	assert "reason=lease_mismatch_or_push_rejected" in result.stdout
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == moved_sha
	assert _remote_sha(fx.origin, "refs/heads/main") == fx.base_sha


@needs_jq
def test_existing_updater_branch_is_updated_with_lease(fx: Fixture) -> None:
	_git(fx.origin, "update-ref", f"refs/heads/{BRANCH}", fx.base_sha)
	fx.record_push(fx.base_sha)
	result = fx.run()
	assert result.returncode == 0, result.stdout + result.stderr
	pushed = _remote_sha(fx.origin, f"refs/heads/{BRANCH}")
	assert pushed != fx.base_sha
	assert fx.outputs()["pushed_sha"] == pushed
	assert _remote_sha(fx.origin, "refs/heads/main") == fx.base_sha


@pytest.mark.parametrize("activity", ["foreign_pusher", "stale_tip", "empty"])
@needs_jq
def test_forged_updater_metadata_without_pat_push_fails_closed(fx: Fixture, activity: str) -> None:
	# Committer email and trailer match the updater, but the authenticated
	# pusher record does not prove the GH_PAT account wrote the branch tip.
	forged = fx.seed_branch("github-actions[bot]@users.noreply.github.com", f"forged\n\nUpdater-Release-SHA: {UPSTREAM_SHA}")
	fx.compare.write_text(json.dumps({"ahead_by": 1, "commits": [{"commit": {"committer": {"email": "github-actions[bot]@users.noreply.github.com"}, "message": f"forged\n\nUpdater-Release-SHA: {UPSTREAM_SHA}"}}]}), encoding="utf-8")
	if activity == "foreign_pusher":
		fx.record_push(forged, login="someone")
	elif activity == "stale_tip":
		fx.record_push(fx.base_sha)
	result = fx.run()
	assert result.returncode != 0
	assert "reason=foreign_commits_on_branch" in result.stdout
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == forged
	assert not any(call.startswith(("pr create", "pr edit", "pr merge")) for call in fx.gh_calls())


@needs_jq
def test_release_tag_resolved_from_git_when_verification_is_off(fx: Fixture) -> None:
	# Verification off leaves RELEASE_TAG empty; the PR body still names the
	# vX.Y.Z tag, resolved over the git protocol on the release clone.
	_git(fx.upstream, "init", "-q", "-b", "stable")
	_git(fx.upstream, "add", "-A")
	_git(fx.upstream, "commit", "-q", "-m", "release")
	release_sha = _git(fx.upstream, "rev-parse", "HEAD")
	_git(fx.upstream, "tag", "-a", "v9.9.9", "-m", "v9.9.9")
	release_origin = fx.tmp / "release.git"
	_git(fx.tmp, "clone", "-q", "--bare", str(fx.upstream), str(release_origin))
	_git(fx.upstream, "remote", "add", "origin", f"file://{release_origin}")
	result = fx.run(verified="skipped", release_tag="", upstream_sha=release_sha)
	assert result.returncode == 0, result.stdout + result.stderr
	body = fx.body_copy.read_text(encoding="utf-8")
	assert "- **Release tag:** v9.9.9" in body
	assert release_sha in body


@needs_jq
def test_release_tag_stays_unresolved_without_a_matching_tag(fx: Fixture) -> None:
	result = fx.run(verified="skipped", release_tag="")
	assert result.returncode == 0, result.stdout + result.stderr
	assert "- **Release tag:** unresolved" in fx.body_copy.read_text(encoding="utf-8")


@needs_jq
def test_pr_closed_by_human_skips_without_pushing(fx: Fixture) -> None:
	fx.pr_list.write_text(json.dumps([{"number": 4, "url": "u", "state": "CLOSED", "isCrossRepository": False, "headRefName": BRANCH, "mergedAt": None}]), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stderr
	assert "reason=closed_by_human" in result.stdout
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == ""
	assert _remote_sha(fx.origin, "refs/heads/main") == fx.base_sha
	assert fx.outputs()["pr_action"] == "skipped"


@needs_jq
def test_only_matching_pat_authored_stale_prs_are_closed(fx: Fixture) -> None:
	fx.stale_list.write_text(json.dumps([
		{"number": 11, "url": "u11", "headRefName": "auto/update-workflows-aaaaaaaaaaaa", "isCrossRepository": False, "author": {"login": PAT_LOGIN}},
		{"number": 12, "url": "u12", "headRefName": "auto/update-workflows-bbbbbbbbbbbb", "isCrossRepository": True, "author": {"login": PAT_LOGIN}},
		{"number": 13, "url": "u13", "headRefName": "auto/update-workflows-cccccccccccc", "isCrossRepository": False, "author": {"login": "human"}},
		{"number": 14, "url": "u14", "headRefName": "auto/update-workflows-old", "isCrossRepository": False, "author": {"login": PAT_LOGIN}},
		{"number": 15, "url": "u15", "headRefName": BRANCH, "isCrossRepository": False, "author": {"login": PAT_LOGIN}},
	]), encoding="utf-8")
	result = fx.run()
	assert result.returncode == 0, result.stderr
	closes = [call for call in fx.gh_calls() if call.startswith("pr close")]
	assert len(closes) == 1 and closes[0].startswith(f"pr close 11 --repo {REPO}")
	assert "https://github.com/octo/consumer/pull/77" in closes[0]
	assert fx.outputs()["stale_closed"] == "1"


@needs_jq
def test_auto_close_keyword_in_commit_message_blocks_delivery(fx: Fixture, monkeypatch) -> None:
	(fx.state / "updated_files.txt").write_text("", encoding="utf-8")
	original = fx.stage_change

	def stage() -> None:
		original()
		(fx.state / "updated_files.txt").write_text("• Fixes #1\n", encoding="utf-8")

	monkeypatch.setattr(fx, "stage_change", stage)
	# The fake gh labels every issue as a tracking issue for this test.
	gh = fx.bin / "gh"
	gh.write_text(FAKE_GH.replace("\"issue view\") echo '[]'", "\"issue view\") echo '[\"ai:orchestrator-tracking\"]'"), encoding="utf-8")
	result = fx.run()
	assert result.returncode != 0
	assert "reason=pr_body_lint_failed" in result.stdout
	assert _remote_sha(fx.origin, f"refs/heads/{BRANCH}") == ""
	assert not any(call.startswith(("pr create", "pr list")) for call in fx.gh_calls())
