#!/usr/bin/env python3
"""plan.yml wiring for the superseded CI-repair issue check (issue #7216).

Contract checks read plan.yml as text. Behavioural checks run the step's own
shell body with a fake `gh` on PATH and the real helper staged under
`.codex-workflow-src/scripts`, replaying the #7185 shape: a re-issue (no
labels, pipeline author) of triage issue #6977 for PR #6976, which merged
with CI green on its final head.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plan.yml"
STEP_NAME = "Close superseded CI-repair issue (merged PR)"
REPO = "acme/widgets"
PIPELINE = "pipeline-bot"
HEAD_SHA = "c" * 40
HEAD_BRANCH = "ai/issue-6969"


IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
HANDOFF_SUPPORT_REF_STEP = "Resolve workflow support ref for CI-triage PR-not-open hand-off"
HANDOFF_CHECKOUT_STEP = "Checkout support source for CI-triage PR-not-open hand-off"
HANDOFF_STEP = "Hand off CI-triage issue whose PR is no longer open"


def _step_names(workflow: Path = PLAN_WORKFLOW) -> list[str]:
	return [line.strip()[len("- name: "):] for line in workflow.read_text().splitlines()
		if line.startswith("      - name: ")]


def _step_block(name: str, workflow: Path = PLAN_WORKFLOW) -> list[str]:
	lines = workflow.read_text().splitlines()
	start = lines.index(f"      - name: {name}")
	end = next(i for i in range(start + 1, len(lines))
		if lines[i].startswith("      - name: ") or lines[i].startswith("      # "))
	return lines[start:end]


def _step_run(name: str, workflow: Path = PLAN_WORKFLOW) -> str:
	block = _step_block(name, workflow)
	run_at = block.index("        run: |")
	return textwrap.dedent("\n".join(block[run_at + 1:])) + "\n"


def test_step_order_and_gating():
	names = _step_names()
	position = names.index(STEP_NAME)
	assert names.index("Skip stale /answer comments") < position
	assert position < names.index("Check and claim /answer command")
	assert position < names.index("Run Codex planning")
	block = "\n".join(_step_block(STEP_NAME))
	assert "        if: env.SKIP_PLAN != 'true'" in block
	assert "PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED: ${{ vars.PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED || 'true' }}" in block


def test_step_runs_only_the_trusted_helper_without_untrusted_expressions():
	body = _step_run(STEP_NAME)
	assert "${{" not in body
	assert 'python3 -I -B "${helper}" classify' in body
	assert ".codex-workflow-src/scripts/plan_superseded_merged_pr.py" in body
	assert "scripts/plan_superseded_merged_pr.py\"" not in body.replace(".codex-workflow-src/scripts/", "").replace(
		".codex-workflow-src-main/scripts/", "")


def test_ref_context_honours_the_reroute():
	ref_ctx = _step_run("Capture planning ref context")
	assert 'HEAD_BRANCH="${PLAN_REROUTED_REF}"' in ref_ctx
	reroute = ref_ctx.index('if [ -n "${PLAN_REROUTED_REF:-}" ]; then\n  REF_MISMATCH="false"')
	assert reroute > ref_ctx.index('REF_MISMATCH="true"')
	build = _step_run("Build planning context")
	assert "MERGED PULL REQUEST FACTS" in build
	assert "Rerouted from merged PR head to base" in build


def test_workflow_parses_as_yaml():
	yaml = pytest.importorskip("yaml")
	steps = yaml.safe_load(PLAN_WORKFLOW.read_text())["jobs"]["plan"]["steps"]
	step = next(step for step in steps if step.get("name") == STEP_NAME)
	assert step["id"] == "superseded_merged_pr"


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as handle:
	handle.write(json.dumps(args) + "\n")
if args[:2] == ["api", "user"]:
	print(os.environ["FAKE_LOGIN"])
	sys.exit(0)
if args[:2] in (["issue", "edit"], ["issue", "close"]):
	sys.exit(1 if os.environ.get("FAKE_EDIT_FAIL") == "1" else 0)
if args and args[0] == "api":
	path = next(arg for arg in args if arg.startswith("repos/"))
	if path.endswith("/comments") and "-f" in args:
		sys.exit(1 if os.environ.get("FAKE_COMMENT_FAIL") == "1" else 0)
	fixtures = json.load(open(os.environ["GH_FIXTURES"], encoding="utf-8"))
	if path in fixtures:
		print(json.dumps(fixtures[path]))
		sys.exit(0)
	print("HTTP 404: Not Found", file=sys.stderr)
	sys.exit(1)
sys.exit(2)
'''


def _triage_body() -> str:
	return "\n".join([
		"<!-- check-failure-triage:pr=6976 -->",
		"",
		f"- **Pull request:** https://github.com/{REPO}/pull/6976 (`{HEAD_BRANCH}`)",
		"- **Failing check:** `CI` (conclusion: `failure`)",
		f"- **Check details:** https://github.com/{REPO}/actions/runs/123",
		"",
		"---",
	])


def _fixtures(run_conclusion: str, merged: bool = True, base_ref: str = "main") -> dict:
	triage = {"number": 6977, "body": _triage_body(), "user": {"login": "owner-human"},
		"author_association": "OWNER", "labels": [{"name": "ai:check-triage"}]}
	return {
		f"repos/{REPO}/issues/6977": triage,
		f"repos/{REPO}/pulls/6976": {"merged_at": "2026-10-10T00:00:00Z" if merged else None, "state": "closed",
			"merged": merged,
			"head": {"sha": HEAD_SHA, "ref": HEAD_BRANCH, "repo": {"full_name": REPO}}, "base": {"ref": base_ref}},
		f"repos/{REPO}/actions/runs/123": {"workflow_id": 77, "repository": {"full_name": REPO}},
		f"repos/{REPO}/actions/workflows/77/runs?head_sha={HEAD_SHA}&per_page=1":
			{"workflow_runs": [{"status": "completed", "conclusion": run_conclusion}]},
	}


def _run_step(tmp_path: Path, *, run_conclusion="success", comments=None, env_extra=None, stage_helper=True,
	issue_body="Re-issued by the unblock judge from #6977.\n\nSpecification: fix CI.\n\nWhy: red.", cwd=None,
	merged=True, base_ref="main"):
	work = cwd or (tmp_path / "work")
	work.mkdir(exist_ok=True)
	if stage_helper:
		support = work / ".codex-workflow-src" / "scripts"
		support.mkdir(parents=True, exist_ok=True)
		for name in ("plan_superseded_merged_pr.py", "files_touched_scope_guard.py"):
			shutil.copy2(REPO_ROOT / "scripts" / name, support / name)
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH)
	gh.chmod(0o755)
	runtime = tmp_path / "runtime"
	runtime.mkdir(exist_ok=True)
	meta = runtime / "issue_meta.json"
	meta.write_text(json.dumps({"number": 7185, "title": "Re-issue of #6977: CI on PR #6976", "body": issue_body,
		"user": {"login": PIPELINE}, "author_association": "NONE", "labels": []}))
	(runtime / "issue_comments.json").write_text(json.dumps(comments or []))
	fixtures = tmp_path / "fixtures.json"
	fixtures.write_text(json.dumps(_fixtures(run_conclusion, merged=merged, base_ref=base_ref)))
	github_env = tmp_path / "github_env"
	github_env.write_text("")
	log = tmp_path / "gh.log"
	env = {
		"PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
		"HOME": str(tmp_path),
		"GH_LOG": str(log), "GH_FIXTURES": str(fixtures), "FAKE_LOGIN": PIPELINE,
		"GITHUB_ENV": str(github_env), "RUNTIME_DIR": str(runtime),
		"ISSUE_META_FILE": str(meta), "ISSUE_COMMENTS_FILE": str(runtime / "issue_comments.json"),
		"ISSUE_NUMBER": "7185", "REPO_SLUG": REPO, "PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED": "true",
		"PLAN_SUPERSEDE_RESOLVED_REF": "main", "PYTHONDONTWRITEBYTECODE": "1",
	}
	env.update(env_extra or {})
	proc = subprocess.run(["bash", "-c", _step_run(STEP_NAME)], cwd=str(work), env=env, text=True,
		capture_output=True, timeout=120)
	calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
	return proc, calls, github_env.read_text()


def test_7185_reissue_is_closed_as_superseded(tmp_path):
	proc, calls, github_env = _run_step(tmp_path)
	output = proc.stdout + proc.stderr
	assert proc.returncode == 0, output
	assert "SKIP_PLAN=true" in github_env
	posts = [call for call in calls if call[0] == "api" and call[1].endswith("/comments") and "-f" in call]
	assert len(posts) == 1
	body = posts[0][posts[0].index("-f") + 1]
	assert f"<!-- ai:plan-superseded:v1 pr=6976 head={HEAD_SHA} -->" in body
	assert HEAD_BRANCH not in body
	edit = next(call for call in calls if call[:2] == ["issue", "edit"])
	assert "ai:closed" in edit and "ai:planning" in edit and "ai:clarification" in edit
	close = next(call for call in calls if call[:2] == ["issue", "close"])
	assert close[close.index("--reason") + 1] == "not planned"
	assert "outcome=superseded label=applied close=done comment=posted" in output


def test_existing_marker_skips_the_post_but_still_closes(tmp_path):
	marker = f"<!-- ai:plan-superseded:v1 pr=6976 head={HEAD_SHA} -->"
	comments = [{"user": {"login": PIPELINE}, "body": f"Earlier.\n{marker}"}]
	proc, calls, github_env = _run_step(tmp_path, comments=comments)
	assert proc.returncode == 0, proc.stderr
	assert not [call for call in calls if call[0] == "api" and call[1].endswith("/comments")]
	assert any(call[:2] == ["issue", "close"] for call in calls)
	assert "SKIP_PLAN=true" in github_env


def test_marker_from_another_author_does_not_count(tmp_path):
	marker = f"<!-- ai:plan-superseded:v1 pr=6976 head={HEAD_SHA} -->"
	comments = [{"user": {"login": "someone"}, "body": marker}]
	proc, calls, _ = _run_step(tmp_path, comments=comments)
	assert any(call[0] == "api" and call[1].endswith("/comments") for call in calls), proc.stdout


def test_comment_failure_plans_as_usual(tmp_path):
	proc, calls, github_env = _run_step(tmp_path, env_extra={"FAKE_COMMENT_FAIL": "1"})
	assert proc.returncode == 0
	assert "SKIP_PLAN" not in github_env
	assert "reason=comment_failed" in proc.stdout
	assert not any(call[:2] == ["issue", "close"] for call in calls)


def test_close_failure_still_skips_planning(tmp_path):
	proc, _, github_env = _run_step(tmp_path, env_extra={"FAKE_EDIT_FAIL": "1"})
	assert proc.returncode == 0
	assert "SKIP_PLAN=true" in github_env
	assert "label=failed close=failed" in proc.stdout


def test_switch_off_makes_no_calls(tmp_path):
	proc, calls, github_env = _run_step(tmp_path, env_extra={"PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED": "False"})
	assert proc.returncode == 0
	assert calls == [] and github_env == ""
	assert "outcome=skip reason=disabled" in proc.stdout


def test_non_candidate_makes_no_calls(tmp_path):
	proc, calls, github_env = _run_step(tmp_path, issue_body="Accepted with this follow-up by the unblock judge (#7185).")
	assert proc.returncode == 0
	assert calls == [] and github_env == ""
	assert "reason=not_candidate" in proc.stdout


def test_missing_helper_plans_as_usual(tmp_path):
	proc, calls, github_env = _run_step(tmp_path, stage_helper=False)
	assert proc.returncode == 0
	assert "SKIP_PLAN" not in github_env
	assert "reason=helper_missing" in proc.stdout
	assert calls == []


def _git(*args, cwd):
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid", "GIT_COMMITTER_NAME": "t",
		"GIT_COMMITTER_EMAIL": "t@e.invalid"})
	subprocess.run(["git", *args], cwd=str(cwd), env=env, check=True, capture_output=True)


def test_merged_check_failed_reroutes_to_base(tmp_path):
	origin = tmp_path / "origin.git"
	seed = tmp_path / "seed"
	work = tmp_path / "work"
	_git("init", "-q", "--bare", str(origin), cwd=tmp_path)
	_git("init", "-q", "-b", "main", str(seed), cwd=tmp_path)
	(seed / "README.md").write_text("base\n")
	_git("add", "README.md", cwd=seed)
	_git("commit", "-q", "-m", "base", cwd=seed)
	_git("push", "-q", str(origin), "main", cwd=seed)
	_git("checkout", "-q", "-b", HEAD_BRANCH, cwd=seed)
	(seed / "README.md").write_text("head\n")
	_git("commit", "-q", "-am", "head", cwd=seed)
	_git("push", "-q", str(origin), HEAD_BRANCH, cwd=seed)
	_git("clone", "-q", "-b", HEAD_BRANCH, str(origin), str(work), cwd=tmp_path)
	proc, calls, github_env = _run_step(tmp_path, run_conclusion="failure", cwd=work,
		env_extra={"PLAN_SUPERSEDE_RESOLVED_REF": HEAD_BRANCH})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "PLAN_REROUTED_REF=main" in github_env
	assert "SKIP_PLAN" not in github_env
	assert (work / "README.md").read_text() == "base\n"
	facts_line = next(line for line in github_env.splitlines() if line.startswith("PLAN_MERGED_PR_FACTS_FILE="))
	facts = Path(facts_line.split("=", 1)[1]).read_text()
	assert "plan against main" in facts and HEAD_BRANCH not in facts
	assert f"outcome=rerouted from={HEAD_BRANCH} to=main" in proc.stdout
	assert not any(call[:2] == ["issue", "close"] for call in calls)


def test_failed_reroute_skips_planning_instead_of_using_the_former_head(tmp_path):
	origin = tmp_path / "origin.git"
	seed = tmp_path / "seed"
	work = tmp_path / "work"
	_git("init", "-q", "--bare", str(origin), cwd=tmp_path)
	_git("init", "-q", "-b", HEAD_BRANCH, str(seed), cwd=tmp_path)
	(seed / "README.md").write_text("head\n")
	_git("add", "README.md", cwd=seed)
	_git("commit", "-q", "-m", "head", cwd=seed)
	# The base branch "main" is absent on origin, so the fetch fails.
	_git("push", "-q", str(origin), HEAD_BRANCH, cwd=seed)
	_git("clone", "-q", "-b", HEAD_BRANCH, str(origin), str(work), cwd=tmp_path)
	proc, calls, github_env = _run_step(tmp_path, run_conclusion="failure", cwd=work,
		env_extra={"PLAN_SUPERSEDE_RESOLVED_REF": HEAD_BRANCH})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "SKIP_PLAN=true" in github_env
	assert "PLAN_REROUTED_REF" not in github_env
	assert "outcome=skip reason=reroute_failed" in proc.stdout
	assert not any(call[:2] == ["issue", "close"] for call in calls)


def test_reroute_keeps_staged_support_copies_that_differ_between_head_and_base(tmp_path):
	# "Stage workflow support files" overwrites tracked scripts/ files; a
	# plain checkout refuses when such a file differs between head and base.
	origin = tmp_path / "origin.git"
	seed = tmp_path / "seed"
	work = tmp_path / "work"
	_git("init", "-q", "--bare", str(origin), cwd=tmp_path)
	_git("init", "-q", "-b", "main", str(seed), cwd=tmp_path)
	(seed / "scripts").mkdir()
	(seed / "README.md").write_text("base\n")
	(seed / "scripts" / "helper.sh").write_text("base helper\n")
	_git("add", "README.md", "scripts/helper.sh", cwd=seed)
	_git("commit", "-q", "-m", "base", cwd=seed)
	_git("push", "-q", str(origin), "main", cwd=seed)
	_git("checkout", "-q", "-b", HEAD_BRANCH, cwd=seed)
	(seed / "README.md").write_text("head\n")
	(seed / "scripts" / "helper.sh").write_text("head helper\n")
	_git("commit", "-q", "-am", "head", cwd=seed)
	_git("push", "-q", str(origin), HEAD_BRANCH, cwd=seed)
	_git("clone", "-q", "-b", HEAD_BRANCH, str(origin), str(work), cwd=tmp_path)
	(work / "scripts" / "helper.sh").write_text("staged helper\n")
	proc, _, github_env = _run_step(tmp_path, run_conclusion="failure", cwd=work,
		env_extra={"PLAN_SUPERSEDE_RESOLVED_REF": HEAD_BRANCH})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "PLAN_REROUTED_REF=main" in github_env, proc.stdout + proc.stderr
	assert "SKIP_PLAN" not in github_env
	assert (work / "README.md").read_text() == "base\n"
	assert (work / "scripts" / "helper.sh").read_text() == "staged helper\n"


def test_reroute_keeps_ignored_staged_helpers_the_base_tracks(tmp_path):
	# Consumer runs list staged helpers in scripts/.gitignore; checkout
	# overwrites an ignored file when the target commit tracks that path.
	origin = tmp_path / "origin.git"
	seed = tmp_path / "seed"
	work = tmp_path / "work"
	_git("init", "-q", "--bare", str(origin), cwd=tmp_path)
	_git("init", "-q", "-b", "main", str(seed), cwd=tmp_path)
	(seed / "scripts").mkdir()
	(seed / "README.md").write_text("base\n")
	(seed / "scripts" / "helper.sh").write_text("base helper\n")
	_git("add", "README.md", "scripts/helper.sh", cwd=seed)
	_git("commit", "-q", "-m", "base", cwd=seed)
	_git("push", "-q", str(origin), "main", cwd=seed)
	_git("checkout", "-q", "-b", HEAD_BRANCH, cwd=seed)
	_git("rm", "-q", "scripts/helper.sh", cwd=seed)
	(seed / "README.md").write_text("head\n")
	_git("commit", "-q", "-am", "head", cwd=seed)
	_git("push", "-q", str(origin), HEAD_BRANCH, cwd=seed)
	_git("clone", "-q", "-b", HEAD_BRANCH, str(origin), str(work), cwd=tmp_path)
	(work / "scripts").mkdir(exist_ok=True)
	(work / "scripts" / "helper.sh").write_text("staged helper\n")
	(work / "scripts" / ".gitignore").write_text("helper.sh\n.gitignore\n")
	proc, _, github_env = _run_step(tmp_path, run_conclusion="failure", cwd=work,
		env_extra={"PLAN_SUPERSEDE_RESOLVED_REF": HEAD_BRANCH})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "PLAN_REROUTED_REF=main" in github_env, proc.stdout + proc.stderr
	assert (work / "README.md").read_text() == "base\n"
	assert (work / "scripts" / "helper.sh").read_text() == "staged helper\n"


# Issue #7477: "Resolve integration ref" now tolerates reason=pr_not_open and
# exports pr_not_open=true; the checkout is then the default branch. The step
# must close the issue, plan on the PR's base, or skip planning.
PR_NOT_OPEN_ENV = {"PLAN_PR_NOT_OPEN": "true", "GITHUB_RUN_ID": "99"}


def _origin_with_branches(tmp_path, default_branch, base_branch=None):
	"""A bare origin holding `default_branch` (and `base_branch` when given),
	plus a work clone checked out on the default branch."""
	origin = tmp_path / "origin.git"
	seed = tmp_path / "seed"
	work = tmp_path / "work"
	_git("init", "-q", "--bare", str(origin), cwd=tmp_path)
	_git("init", "-q", "-b", default_branch, str(seed), cwd=tmp_path)
	(seed / "README.md").write_text("default\n")
	_git("add", "README.md", cwd=seed)
	_git("commit", "-q", "-m", "default", cwd=seed)
	_git("push", "-q", str(origin), default_branch, cwd=seed)
	if base_branch and base_branch != default_branch:
		_git("checkout", "-q", "-b", base_branch, cwd=seed)
		(seed / "README.md").write_text("base\n")
		_git("commit", "-q", "-am", "base", cwd=seed)
		_git("push", "-q", str(origin), base_branch, cwd=seed)
	_git("clone", "-q", "-b", default_branch, str(origin), str(work), cwd=tmp_path)
	return work


def test_pr_not_open_reroutes_to_non_default_base(tmp_path):
	work = _origin_with_branches(tmp_path, "trunk", "main")
	proc, calls, github_env = _run_step(tmp_path, run_conclusion="failure", cwd=work,
		env_extra={**PR_NOT_OPEN_ENV, "PLAN_DEFAULT_BRANCH": "trunk", "PLAN_SUPERSEDE_RESOLVED_REF": "trunk"})
	output = proc.stdout + proc.stderr
	assert proc.returncode == 0, output
	assert "PLAN_REROUTED_REF=main" in github_env, output
	assert "SKIP_PLAN" not in github_env
	assert (work / "README.md").read_text() == "base\n"
	assert f"outcome=rerouted from={HEAD_BRANCH} to=main" in proc.stdout
	assert not any(call[:2] == ["issue", "close"] for call in calls)


def test_pr_not_open_plans_on_default_base(tmp_path):
	proc, calls, github_env = _run_step(tmp_path, run_conclusion="failure",
		env_extra={**PR_NOT_OPEN_ENV, "PLAN_DEFAULT_BRANCH": "main", "PLAN_SUPERSEDE_RESOLVED_REF": "main"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "SKIP_PLAN" not in github_env
	assert "PLAN_REROUTED_REF" not in github_env
	assert "outcome=plan reason=merged_check_not_passed" in proc.stdout
	assert not any(call[:2] == ["issue", "close"] for call in calls)


def _assert_plan_blocked_handoff(calls, pr="6976"):
	# Issue #7995: the skip must move the issue off ai:planning, or stall
	# recovery re-posts /answer every hour and planning skips forever.
	edit = next(call for call in calls if call[:2] == ["issue", "edit"])
	assert edit[edit.index("--add-label") + 1] == "ai:blocked"
	assert edit[edit.index("--remove-label") + 1] == "ai:planning"
	assert not any(call[:2] == ["issue", "close"] for call in calls)
	posts = [call for call in calls if call[0] == "api" and call[1].endswith("/comments") and "-f" in call]
	assert len(posts) == 1
	body = posts[0][posts[0].index("-f") + 1]
	assert body.rstrip().endswith(f"<!-- ai:plan-pr-not-open:v1 pr={pr} run=99 -->")


def test_pr_not_open_closed_unmerged_skips_planning(tmp_path):
	proc, calls, github_env = _run_step(tmp_path, merged=False,
		env_extra={**PR_NOT_OPEN_ENV, "PLAN_DEFAULT_BRANCH": "main"})
	output = proc.stdout + proc.stderr
	assert proc.returncode == 0, output
	assert "SKIP_PLAN=true" in github_env
	assert "outcome=skip reason=pr_not_open_unresolved detail=pr_closed_unmerged pr=6976 label=applied comment=posted" in proc.stdout
	assert output.count("::warning::") == 1, output
	_assert_plan_blocked_handoff(calls)


def test_pr_not_open_handoff_label_failure_posts_no_comment(tmp_path):
	proc, calls, github_env = _run_step(tmp_path, merged=False,
		env_extra={**PR_NOT_OPEN_ENV, "PLAN_DEFAULT_BRANCH": "main", "FAKE_EDIT_FAIL": "1"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "SKIP_PLAN=true" in github_env
	assert "label=failed comment=skipped" in proc.stdout
	assert not [call for call in calls if call[0] == "api" and call[1].endswith("/comments") and "-f" in call]


def test_pr_not_open_handoff_comment_failure_is_logged(tmp_path):
	# The label landed but the marker comment failed: the log must say so.
	proc, calls, github_env = _run_step(tmp_path, merged=False,
		env_extra={**PR_NOT_OPEN_ENV, "PLAN_DEFAULT_BRANCH": "main", "FAKE_COMMENT_FAIL": "1"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "SKIP_PLAN=true" in github_env
	assert "label=applied comment=failed" in proc.stdout
	edit = next(call for call in calls if call[:2] == ["issue", "edit"])
	assert edit[edit.index("--add-label") + 1] == "ai:blocked"


def test_pr_not_open_handoff_uses_the_resolver_pr_before_the_classifier(tmp_path):
	# "disabled" exits before the classifier runs, so only the ref step's PR is known.
	proc, calls, _ = _run_step(tmp_path, env_extra={**PR_NOT_OPEN_ENV, "PLAN_DEFAULT_BRANCH": "main",
		"PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED": "off", "PLAN_PR_NOT_OPEN_PR": "6976"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "detail=disabled pr=6976" in proc.stdout
	_assert_plan_blocked_handoff(calls)


def test_closed_unmerged_without_pr_not_open_still_plans(tmp_path):
	proc, _, github_env = _run_step(tmp_path, merged=False)
	assert proc.returncode == 0
	assert "SKIP_PLAN" not in github_env
	assert "outcome=plan reason=pr_closed_unmerged" in proc.stdout


@pytest.mark.parametrize("variant,detail", [
	("helper_missing", "helper_missing"),
	("comment_failed", "comment_failed"),
	("disabled", "disabled"),
	("not_candidate", "not_candidate"),
])
def test_pr_not_open_unverifiable_skips_planning(tmp_path, variant, detail):
	env_extra = {**PR_NOT_OPEN_ENV, "PLAN_DEFAULT_BRANCH": "main"}
	kwargs = {}
	if variant == "helper_missing":
		kwargs["stage_helper"] = False
	elif variant == "comment_failed":
		env_extra["FAKE_COMMENT_FAIL"] = "1"
	elif variant == "disabled":
		env_extra["PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED"] = "False"
	elif variant == "not_candidate":
		kwargs["issue_body"] = "Plain issue body."
	proc, calls, github_env = _run_step(tmp_path, env_extra=env_extra, **kwargs)
	output = proc.stdout + proc.stderr
	assert proc.returncode == 0, output
	assert "SKIP_PLAN=true" in github_env
	assert f"outcome=skip reason=pr_not_open_unresolved detail={detail}" in proc.stdout
	assert output.count("::warning::") == 1, output
	assert not any(call[:2] == ["issue", "close"] for call in calls)
	edit = next(call for call in calls if call[:2] == ["issue", "edit"])
	assert edit[edit.index("--add-label") + 1] == "ai:blocked"


def test_pr_not_open_failed_reroute_skips(tmp_path):
	# The base branch "main" is absent on origin, so the fetch fails.
	work = _origin_with_branches(tmp_path, "trunk")
	proc, calls, github_env = _run_step(tmp_path, run_conclusion="failure", cwd=work,
		env_extra={**PR_NOT_OPEN_ENV, "PLAN_DEFAULT_BRANCH": "trunk", "PLAN_SUPERSEDE_RESOLVED_REF": "trunk"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "SKIP_PLAN=true" in github_env
	assert "PLAN_REROUTED_REF" not in github_env
	assert "outcome=skip reason=pr_not_open_unresolved detail=reroute_failed" in proc.stdout
	_assert_plan_blocked_handoff(calls)


def test_pr_not_open_invalid_base_ref_skips(tmp_path):
	proc, _, github_env = _run_step(tmp_path, run_conclusion="failure", base_ref="bad..ref",
		env_extra={**PR_NOT_OPEN_ENV, "PLAN_DEFAULT_BRANCH": "main"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "SKIP_PLAN=true" in github_env
	assert "PLAN_REROUTED_REF" not in github_env
	# classify() rejects "bad..ref" itself (_valid_branch) as pr_read_failed,
	# so the shell's invalid_base_ref branch stays defence in depth here.
	assert "reason=pr_not_open_unresolved detail=pr_read_failed" in proc.stdout


def test_superseded_still_closes_on_pr_not_open_path(tmp_path):
	proc, calls, github_env = _run_step(tmp_path, env_extra={**PR_NOT_OPEN_ENV, "PLAN_DEFAULT_BRANCH": "main"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "SKIP_PLAN=true" in github_env
	assert "outcome=superseded" in proc.stdout
	assert "pr_not_open_unresolved" not in proc.stdout
	assert any(call[:2] == ["issue", "close"] for call in calls)


# Issue #7540: implement.yml's "Resolve integration ref" skips on a CI-triage
# PR that is no longer open; the hand-off step closes the issue as superseded
# or labels it ai:blocked, from the verified support copy only.


def test_implement_handoff_steps_order_and_gating():
	names = _step_names(IMPLEMENT_WORKFLOW)
	refctx = names.index("Resolve integration ref")
	ordered = [names.index(n) for n in (HANDOFF_SUPPORT_REF_STEP, HANDOFF_CHECKOUT_STEP, HANDOFF_STEP)]
	assert refctx < ordered[0] < ordered[1] < ordered[2] < names.index("Resolve checkout ref")
	for name in (HANDOFF_SUPPORT_REF_STEP, HANDOFF_CHECKOUT_STEP, HANDOFF_STEP):
		block = "\n".join(_step_block(name, IMPLEMENT_WORKFLOW))
		assert "        if: steps.refctx.outputs.pr_not_open == 'true'" in block
	checkout = "\n".join(_step_block(HANDOFF_CHECKOUT_STEP, IMPLEMENT_WORKFLOW))
	assert "persist-credentials: false" in checkout and "sparse-checkout: scripts" in checkout
	assert "path: .codex-workflow-src" in checkout


def test_implement_handoff_uses_only_the_trusted_helper():
	# The support-ref step inlines github.repository / github.sha like its
	# siblings (test_workflow_support_ref_follows_pin.py); the hand-off body
	# itself takes every value through env:.
	assert "${{" not in _step_run(HANDOFF_STEP, IMPLEMENT_WORKFLOW)
	checkout = "\n".join(_step_block(HANDOFF_CHECKOUT_STEP, IMPLEMENT_WORKFLOW))
	assert "ref: ${{ env.PR_NOT_OPEN_SCRIPT_REF }}" in checkout
	body = _step_run(HANDOFF_STEP, IMPLEMENT_WORKFLOW)
	assert 'python3 -I -B "${helper}" classify' in body
	assert 'sup="$(pwd)/.codex-workflow-src/scripts"' in body
	assert "source scripts/" not in body and "bash scripts/" not in body
	assert "RESTORE_AWAITING_APPROVAL_ON_SKIP" not in body
	assert "git checkout" not in body


def test_implement_workflow_parses_with_handoff_step():
	yaml = pytest.importorskip("yaml")
	steps = yaml.safe_load(IMPLEMENT_WORKFLOW.read_text())["jobs"]["implement"]["steps"]
	step = next(step for step in steps if step.get("name") == HANDOFF_STEP)
	assert step["id"] == "pr_not_open_handoff"


def _run_handoff(tmp_path: Path, *, run_conclusion="success", merged=True, comments_fixture=None,
	env_extra=None, stage_helper=True):
	work = tmp_path / "work"
	work.mkdir(exist_ok=True)
	if stage_helper:
		support = work / ".codex-workflow-src" / "scripts"
		support.mkdir(parents=True, exist_ok=True)
		for name in ("plan_superseded_merged_pr.py", "files_touched_scope_guard.py"):
			shutil.copy2(REPO_ROOT / "scripts" / name, support / name)
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH)
	gh.chmod(0o755)
	runtime = tmp_path / "runtime"
	runtime.mkdir(exist_ok=True)
	meta = runtime / "issue_meta.json"
	meta.write_text(json.dumps({"number": 7185, "title": "Re-issue of #6977: CI on PR #6976",
		"body": "Re-issued by the unblock judge from #6977.\n\nSpecification: fix CI.",
		"user": {"login": PIPELINE}, "author_association": "NONE", "labels": []}))
	fixtures_data = _fixtures(run_conclusion, merged=merged)
	if comments_fixture is not None:
		fixtures_data[f"repos/{REPO}/issues/7185/comments?per_page=100"] = comments_fixture
	fixtures = tmp_path / "fixtures.json"
	fixtures.write_text(json.dumps(fixtures_data))
	log = tmp_path / "gh.log"
	env = {
		"PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
		"HOME": str(tmp_path),
		"GH_LOG": str(log), "GH_FIXTURES": str(fixtures), "FAKE_LOGIN": PIPELINE,
		"RUNTIME_DIR": str(runtime), "ISSUE_META_FILE": str(meta),
		"ISSUE_NUMBER": "7185", "REPO_SLUG": REPO, "HANDOFF_PR": "6976", "GITHUB_RUN_ID": "99",
		"PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED": "true", "PYTHONDONTWRITEBYTECODE": "1",
	}
	env.update(env_extra or {})
	proc = subprocess.run(["bash", "-c", _step_run(HANDOFF_STEP, IMPLEMENT_WORKFLOW)], cwd=str(work), env=env,
		text=True, capture_output=True, timeout=120)
	calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
	return proc, calls


def _posted_bodies(calls):
	return [call[call.index("-f") + 1] for call in calls
		if call[0] == "api" and call[1].endswith("/comments") and "-f" in call]


def test_implement_handoff_closes_superseded_issue(tmp_path):
	proc, calls = _run_handoff(tmp_path)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	bodies = _posted_bodies(calls)
	assert len(bodies) == 1
	assert f"<!-- ai:plan-superseded:v1 pr=6976 head={HEAD_SHA} -->" in bodies[0]
	edit = next(call for call in calls if call[:2] == ["issue", "edit"])
	assert "ai:closed" in edit and "ai:awaiting-approval" in edit
	assert edit[edit.index("--add-label") + 1] == "ai:closed"
	close = next(call for call in calls if call[:2] == ["issue", "close"])
	assert close[close.index("--reason") + 1] == "not planned"
	assert "gate=ci_triage_pr_not_open reason=superseded outcome=closed label=applied close=done comment=posted" in proc.stdout


def test_implement_handoff_existing_marker_is_not_posted_again(tmp_path):
	marker = f"<!-- ai:plan-superseded:v1 pr=6976 head={HEAD_SHA} -->"
	pages = [[{"user": {"login": PIPELINE}, "body": f"Earlier.\n{marker}"}]]
	proc, calls = _run_handoff(tmp_path, comments_fixture=pages)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _posted_bodies(calls) == []
	assert any(call[:2] == ["issue", "close"] for call in calls)
	assert "comment=present" in proc.stdout


def _assert_blocked(proc, calls, detail):
	assert proc.returncode == 0, proc.stdout + proc.stderr
	edit = next(call for call in calls if call[:2] == ["issue", "edit"])
	assert edit[edit.index("--add-label") + 1] == "ai:blocked"
	assert edit[edit.index("--remove-label") + 1] == "ai:awaiting-approval"
	assert not any(call[:2] == ["issue", "close"] for call in calls)
	bodies = _posted_bodies(calls)
	assert len(bodies) == 1
	assert bodies[0].rstrip().endswith("<!-- ai:implement-pr-not-open:v1 pr=6976 run=99 -->")
	assert f"reason=blocked_handoff detail={detail} outcome=handed_off" in proc.stdout


def test_implement_handoff_closed_unmerged_is_blocked(tmp_path):
	proc, calls = _run_handoff(tmp_path, merged=False)
	_assert_blocked(proc, calls, "pr_closed_unmerged")


def test_implement_handoff_merged_with_failed_check_is_blocked(tmp_path):
	proc, calls = _run_handoff(tmp_path, run_conclusion="failure")
	_assert_blocked(proc, calls, "merged_check_not_passed")


def test_implement_handoff_missing_helper_is_blocked(tmp_path):
	proc, calls = _run_handoff(tmp_path, stage_helper=False)
	_assert_blocked(proc, calls, "helper_missing")
	assert not any(call[:2] == ["api", "user"] for call in calls)


def test_implement_handoff_switch_off_is_blocked_without_classifier_reads(tmp_path):
	proc, calls = _run_handoff(tmp_path, env_extra={"PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED": "OFF"})
	_assert_blocked(proc, calls, "disabled")
	assert not any(call[0] == "api" and "pulls" in call[1] for call in calls)


def test_implement_handoff_edit_failure_still_exits_zero(tmp_path):
	proc, calls = _run_handoff(tmp_path, merged=False, env_extra={"FAKE_EDIT_FAIL": "1"})
	assert proc.returncode == 0
	assert "label=failed comment=skipped" in proc.stdout
	assert "::warning::" in proc.stdout + proc.stderr
	# No hand-off comment while the label is missing, so a retry cannot duplicate it.
	assert _posted_bodies(calls) == []
