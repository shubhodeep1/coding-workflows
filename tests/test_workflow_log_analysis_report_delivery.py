#!/usr/bin/env python3
"""Contract tests for workflow-log-analysis report delivery through a PR (#8963).

Since 2026-10-26 the default-branch ruleset requires `lint` and
`ai-review/head-gate` with no bypass actors, so the old direct push of the
report to main failed with GH013 on every run. Each run now pushes its report
and sections to `auto/workflow-log-analysis-report-<run_id>-<run_attempt>`,
and the `deliver-report` job opens one PR for it through
scripts/workflow_log_analysis_deliver_report.sh. The 30-day purge deletes a
doc only once analysis/recommendation-processing-report.md lists it.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WF_PATH = REPO_ROOT / ".github" / "workflows" / "workflow-log-analysis.yml"
SCRIPT_PATH = REPO_ROOT / "scripts" / "workflow_log_analysis_deliver_report.sh"
RUN_HEADER = "        run: |\n"
RUN_INDENT = " " * 10
RUN_BRANCH = "auto/workflow-log-analysis-report-100-1"
REPORT = "analysis/workflow-optimization-2026-11-07.md"
SENTINEL_TOKEN = "ghp_SENTINEL_report_push_token_8963"
COMMIT_STEPS = (
	"Commit and push report",
	"Commit and push deep audit section",
	"Commit and push API redundancy section",
)
DELIVER_IF = "if: ${{ always() && github.event_name != 'schedule' && needs.analyze-commit-notify.outputs.committed == 'true' }}"


def _wf() -> str:
	return WF_PATH.read_text(encoding="utf-8")


def _step_block(name: str) -> str:
	wf = _wf()
	marker = f"      - name: {name}\n"
	assert wf.count(marker) == 1, name
	return wf.split(marker, 1)[1].split("\n      - name: ", 1)[0].split("\n  # ", 1)[0]


def _step_run(name: str) -> str:
	block = _step_block(name)
	assert RUN_HEADER in block, name
	lines: list[str] = []
	for line in block.split(RUN_HEADER, 1)[1].splitlines():
		if line.startswith(RUN_INDENT):
			lines.append(line[len(RUN_INDENT):])
		elif line.strip() == "":
			lines.append("")
		else:
			break
	return "\n".join(lines) + "\n"


def _job_block(job_id: str) -> str:
	wf = _wf()
	marker = f"\n  {job_id}:\n"
	assert marker in wf, job_id
	rest = wf.split(marker, 1)[1]
	for line_no, line in enumerate(rest.splitlines()):
		if line_no and line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":") and not line.lstrip().startswith("#"):
			return "\n".join(rest.splitlines()[:line_no])
	return rest


# --- static workflow contract ---------------------------------------------

def test_no_commit_step_pushes_to_the_default_branch() -> None:
	wf = _wf()
	assert 'git push origin "HEAD:${TARGET_BRANCH}"' not in wf
	for name in COMMIT_STEPS:
		body = _step_run(name)
		assert 'git pull --rebase' not in body, name
		assert 'HEAD:${TARGET_BRANCH}' not in body, name
		assert 'git push origin "HEAD:refs/heads/${REPORT_BRANCH}"' in body, name
		assert "GH006|GH013|protected branch|push declined|repository rule" in body, name
		assert "WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=push_rejected_by_ruleset" in body, name
		assert "sed -E 's#(https?://)[^/@[:space:]]+@#\\1***@#g'" in body, name


def test_report_branch_is_per_run_and_validated() -> None:
	body = _step_run("Commit and push report")
	assert 'REPORT_BRANCH="auto/workflow-log-analysis-report-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in body
	assert '[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]]' in body
	assert 'echo "report_branch=${REPORT_BRANCH}" >> "$GITHUB_OUTPUT"' in body
	assert 'echo "purged_docs=${purged_docs_list}" >> "$GITHUB_OUTPUT"' in body
	assert "reason=push_failed" in body
	for name in ("Commit and push deep audit section", "Commit and push API redundancy section"):
		block = _step_block(name)
		assert "REPORT_BRANCH: ${{ needs.analyze-commit-notify.outputs.report_branch }}" in block
		body = _step_run(name)
		assert "LOCAL_PATCH" not in body and "git am" not in body and "reset --hard" not in body
		assert "outcome=skipped reason=no_report_branch" in body
		assert body.index("reason=no_report_branch") < body.index("git push origin")


def test_sync_steps_read_the_run_branch_when_present() -> None:
	wf = _wf()
	marker = "      - name: Sync latest report from target branch\n"
	assert wf.count(marker) == 2
	for block in wf.split(marker)[1:]:
		block = block.split("\n      - name: ", 1)[0]
		assert "REPORT_BRANCH: ${{ needs.analyze-commit-notify.outputs.report_branch }}" in block
		assert '^auto/workflow-log-analysis-report-[0-9]+-[0-9]+$' in block
		assert 'git fetch origin "+refs/heads/${REPORT_BRANCH}:refs/remotes/origin/${REPORT_BRANCH}"' in block
		assert 'git checkout -B "${REPORT_BRANCH}" "origin/${REPORT_BRANCH}"' in block
		# The no-report-branch fallback is unchanged.
		assert 'git pull --rebase origin "${TARGET_BRANCH}"' in block


def test_analyze_job_exposes_delivery_outputs_alongside_existing_ones() -> None:
	job = _job_block("analyze-commit-notify")
	assert "report_file: ${{ steps.commit_report.outputs.report_file || steps.analyze.outputs.report_file }}" in job
	assert "analysis_status: ${{ steps.analyze.outputs.analysis_status }}" in job
	assert "committed: ${{ steps.commit_report.outputs.committed }}" in job
	assert "report_branch: ${{ steps.commit_report.outputs.report_branch }}" in job
	assert "purged_docs: ${{ steps.commit_report.outputs.purged_docs }}" in job
	assert "TG_REPORT_BRANCH: ${{ steps.commit_report.outputs.report_branch }}" in job
	assert "a report PR into ${GITHUB_REF_NAME} is being opened" in job


def test_deliver_report_job_contract() -> None:
	job = _job_block("deliver-report")
	assert "needs: [analyze-commit-notify, deep-audit, api-redundancy]" in job
	assert DELIVER_IF in job
	assert "permissions:\n      contents: read" in job
	assert "contents: write" not in job
	assert "uses: actions/checkout@v5" in job
	assert "persist-credentials: false" in job
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in job
	assert "REPORT_PUSH_TOKEN: ${{ secrets.GH_PAT }}" in job
	# The checkout may fall back to github.token; the delivery step may not.
	checkout = job.split("      - name: Checkout repository\n", 1)[1].split("\n      - name: ", 1)[0]
	assert "token: ${{ secrets.GH_PAT || github.token }}" in checkout
	deliver_step = job.split("      - name: Deliver report pull request\n", 1)[1]
	assert "|| github.token" not in deliver_step
	assert "REPORT_BRANCH: ${{ needs.analyze-commit-notify.outputs.report_branch }}" in job
	assert "PURGED_DOCS: ${{ needs.analyze-commit-notify.outputs.purged_docs }}" in job
	assert "run: bash scripts/workflow_log_analysis_deliver_report.sh" in job
	assert "codex" not in job.lower()


def test_purge_skips_unprocessed_docs_before_git_rm() -> None:
	body = _step_run("Commit and push report")
	assert 'purge_processing_report="analysis/recommendation-processing-report.md"' in body
	assert "WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc=all reason=processing_report_missing" in body
	assert 'if [ "${purge_guard_ok}" = "true" ] && cutoff_date=' in body
	loop = body.split('for old_report in analysis/workflow-optimization-*.md; do', 1)[1]
	assert 'WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc=${old_report} reason=in_flight_project' in loop
	assert loop.index('grep -qxF -- "${old_report}"') < loop.index('git rm -- "${old_report}"')
	assert loop.index('grep -qF -- "${filename}" "${purge_processing_report}"') < loop.index('git rm -- "${old_report}"')
	assert loop.index("reason=unprocessed") < loop.index('git rm -- "${old_report}"')


def test_delivery_script_static_contract() -> None:
	text = SCRIPT_PATH.read_text(encoding="utf-8")
	assert text.startswith("#!/usr/bin/env bash\n")
	assert "set -euo pipefail" in text
	assert "export GIT_CONFIG_KEY_0=credential.helper" in text
	assert "git worktree add --detach" in text
	lint_at = text.index('python3 "${SCRIPT_DIR}/lint_pr_body_auto_close.py"')
	assert lint_at < text.index('push origin "HEAD:refs/heads/${REPORT_BRANCH}"')
	assert lint_at < text.index("gh pr create --repo")
	assert "gh pr merge" not in text and "--delete-branch" not in text
	assert "pulls?state=open&base=${TARGET_BRANCH}&per_page=100" in text
	for line in text.splitlines():
		assert not line.startswith("    "), f"script must indent with tabs: {line!r}"


# --- git sandbox helpers -----------------------------------------------------

def _env(tmp: Path, **extra: str) -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_OUTPUT")}
	home = tmp / "home"
	home.mkdir(exist_ok=True)
	env.update({
		"HOME": str(home),
		"GIT_CONFIG_NOSYSTEM": "1",
		"GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	(home / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
	env.update(extra)
	return env


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
	proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
	assert proc.returncode == 0, (args, proc.stderr)
	return proc.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _origin(tmp: Path, env: dict[str, str], main_files: dict[str, str]) -> tuple[str, Path]:
	bare = tmp / "origin.git"
	_git(tmp, env, "init", "-q", "--bare", "-b", "main", str(bare))
	seed = tmp / "seed"
	_git(tmp, env, "init", "-q", "-b", "main", str(seed))
	_write(seed, "README.md", "seed\n")
	for rel, text in main_files.items():
		_write(seed, rel, text)
	_git(seed, env, "add", "-A")
	_git(seed, env, "commit", "-q", "-m", "seed")
	url = f"file://{bare}"
	_git(seed, env, "remote", "add", "origin", url)
	_git(seed, env, "push", "-q", "origin", "main")
	return url, seed


def _push_branch(seed: Path, env: dict[str, str], branch: str, files: dict[str, str]) -> None:
	_git(seed, env, "checkout", "-q", "-B", branch, "main")
	for rel, text in files.items():
		_write(seed, rel, text)
	_git(seed, env, "add", "-A")
	_git(seed, env, "commit", "-q", "-m", f"files for {branch}")
	_git(seed, env, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
	_git(seed, env, "checkout", "-q", "main")


def _ls_remote_file(tmp: Path, env: dict[str, str], ref: str, rel: str) -> bool:
	proc = subprocess.run(["git", "cat-file", "-e", f"{ref}:{rel}"], cwd=tmp / "origin.git", env=env, capture_output=True)
	return proc.returncode == 0


_FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_GH_LOG}"
if [ "$1" = "api" ]; then
  if [ "${FAKE_PR_LIST_FAIL:-0}" = "1" ]; then echo "HTTP 500" >&2; exit 1; fi
  cat "${FAKE_PR_LIST}"
  exit 0
fi
if [ "$1 $2" = "pr create" ]; then
  prev=""
  for a in "$@"; do
    if [ "${prev}" = "--body-file" ]; then cp "${a}" "${FAKE_BODY_COPY}"; fi
    prev="${a}"
  done
  if [ "${FAKE_CREATE_FAIL:-0}" = "1" ]; then echo "create failed for https://x-access-token:leak@github.com/o/r" >&2; exit 1; fi
  echo "https://github.com/owner/repo/pull/123"
  exit 0
fi
if [ "$1 $2" = "pr list" ]; then
  printf '%s\\n' "${FAKE_LIST_NUMBER:-}"
  exit 0
fi
exit 0
"""


def _delivery_sandbox(tmp: Path, main_files: dict[str, str], others: dict[str, dict[str, str]] | None = None,
		pr_list: str | None = None, run_file: str = REPORT) -> dict[str, str]:
	env = _env(tmp)
	url, seed = _origin(tmp, env, main_files)
	_push_branch(seed, env, RUN_BRANCH, {run_file: "# report\n\n## Deep audit\n"})
	for branch, files in (others or {}).items():
		_push_branch(seed, env, branch, files)
	_git(tmp, env, "clone", "-q", "--depth", "1", "--branch", "main", url, str(tmp / "checkout"))
	trusted = tmp / "trusted" / "scripts"
	trusted.mkdir(parents=True)
	for name in ("workflow_log_analysis_deliver_report.sh", "gh_helpers.sh", "lint_pr_body_auto_close.py"):
		shutil.copy2(REPO_ROOT / "scripts" / name, trusted / name)
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(_FAKE_GH, encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
	(tmp / "prs.json").write_text(pr_list if pr_list is not None else "[]", encoding="utf-8")
	(tmp / "runner").mkdir()
	(tmp / "gh.log").write_text("", encoding="utf-8")
	env.update({
		"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
		"FAKE_GH_LOG": str(tmp / "gh.log"),
		"FAKE_PR_LIST": str(tmp / "prs.json"),
		"FAKE_BODY_COPY": str(tmp / "body.md"),
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"GITHUB_REF_TYPE": "branch",
		"GITHUB_REF_NAME": "main",
		"GITHUB_REPOSITORY": "owner/repo",
		"GITHUB_RUN_ID": "100",
		"GITHUB_SERVER_URL": "https://github.com",
		"RUNNER_TEMP": str(tmp / "runner"),
		"GITHUB_OUTPUT": str(tmp / "github_output"),
		"REPORT_BRANCH": RUN_BRANCH,
		"REPORT_FILE": REPORT,
		"REPORT_PUSH_TOKEN": SENTINEL_TOKEN,
		"GH_TOKEN": SENTINEL_TOKEN,
		"PURGED_DOCS": "analysis/workflow-optimization-2026-09-01.md ../evil.md",
		"AUDIT_STATUS": "completed",
		"API_REDUNDANCY_STATUS": "",
	})
	return env


def _deliver(tmp: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["bash", str(tmp / "trusted" / "scripts" / "workflow_log_analysis_deliver_report.sh")],
		cwd=tmp / "checkout", env=env, capture_output=True, text=True, timeout=120,
	)


def _out(proc: subprocess.CompletedProcess[str]) -> str:
	return proc.stdout + proc.stderr


# --- delivery script behaviour ------------------------------------------------

def test_delivery_opens_pr_without_collision() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {"analysis/workflow-optimization-2026-11-06.md": "old\n"})
		proc = _deliver(tmp, env)
		out = _out(proc)
		assert proc.returncode == 0, out
		assert f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=pr_opened branch={RUN_BRANCH} pr=123 file={REPORT}" in out
		assert SENTINEL_TOKEN not in out
		calls = (tmp / "gh.log").read_text(encoding="utf-8")
		assert "pulls?state=open&base=main&per_page=100" in calls
		assert f"pr create --repo owner/repo --base main --head {RUN_BRANCH}" in calls
		assert "pr list" not in calls
		body = (tmp / "body.md").read_text(encoding="utf-8")
		assert "https://github.com/owner/repo/actions/runs/100" in body
		assert "`analysis/workflow-optimization-2026-09-01.md`" in body
		assert "evil" not in body
		assert "Deep audit section: completed" in body
		assert "API redundancy section: not run" in body
		output = (tmp / "github_output").read_text(encoding="utf-8")
		assert "pr_number=123" in output and f"report_file={REPORT}" in output
		assert not list((tmp / "runner").iterdir()), "work dir must be cleaned up"


def test_delivery_renames_on_collision_with_default_branch() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {REPORT: "someone else's report\n"})
		proc = _deliver(tmp, env)
		out = _out(proc)
		assert proc.returncode == 0, out
		renamed = REPORT.replace(".md", "-2.md")
		assert f"pr=123 file={renamed}" in out
		assert _ls_remote_file(tmp, env, RUN_BRANCH, renamed)
		assert not _ls_remote_file(tmp, env, RUN_BRANCH, REPORT)
		assert _ls_remote_file(tmp, env, "main", REPORT)
		assert "workflow-optimization-2026-11-07-2.md)" in (tmp / "gh.log").read_text(encoding="utf-8")


def test_delivery_renames_past_other_open_report_branches() -> None:
	other = "auto/workflow-log-analysis-report-99-1"
	pr_list = (
		'[{"head": {"ref": "%s", "repo": {"full_name": "owner/repo"}}},'
		' {"head": {"ref": "auto/workflow-log-analysis-report-98-1", "repo": {"full_name": "fork/repo"}}},'
		' {"head": {"ref": "feature/x", "repo": {"full_name": "owner/repo"}}}]' % other
	)
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {REPORT: "x\n"}, others={other: {REPORT.replace(".md", "-2.md"): "y\n"}}, pr_list=pr_list)
		proc = _deliver(tmp, env)
		out = _out(proc)
		assert proc.returncode == 0, out
		assert "fetch_partial" not in out, "fork and non-report heads must not be fetched"
		assert f"file={REPORT.replace('.md', '-3.md')}" in out


def test_delivery_rerun_after_rename_reuses_the_open_pr() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {REPORT: "x\n"})
		first = _deliver(tmp, env)
		assert first.returncode == 0, _out(first)
		env.update({"FAKE_CREATE_FAIL": "1", "FAKE_LIST_NUMBER": "123"})
		second = _deliver(tmp, env)
		out = _out(second)
		assert second.returncode == 0, out
		assert f"pr=123 file={REPORT.replace('.md', '-2.md')}" in out
		assert "x-access-token:leak@" not in out


def test_delivery_fails_closed_without_token() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {})
		env["REPORT_PUSH_TOKEN"] = ""
		proc = _deliver(tmp, env)
		out = _out(proc)
		assert proc.returncode == 1
		assert f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=token_missing branch={RUN_BRANCH}" in out
		assert SENTINEL_TOKEN not in out
		assert (tmp / "gh.log").read_text(encoding="utf-8") == ""


def test_delivery_rejects_invalid_branch_and_file() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {})
		bad = dict(env, REPORT_BRANCH="main")
		assert "reason=report_branch_invalid" in _out(_deliver(tmp, bad))
		bad = dict(env, REPORT_FILE="analysis/../x.md")
		assert "reason=report_file_invalid" in _out(_deliver(tmp, bad))


def test_delivery_falls_back_to_pr_list_when_create_fails() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {})
		env.update({"FAKE_CREATE_FAIL": "1", "FAKE_LIST_NUMBER": "77"})
		proc = _deliver(tmp, env)
		assert proc.returncode == 0, _out(proc)
		assert "outcome=pr_opened" in proc.stdout and "pr=77" in proc.stdout
		env["FAKE_LIST_NUMBER"] = ""
		proc = _deliver(tmp, env)
		out = _out(proc)
		assert proc.returncode == 1
		assert "reason=pr_create_failed" in out
		assert "x-access-token:leak@" not in out and "***@github.com" in out


def test_delivery_degrades_when_pr_listing_fails() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {})
		env["FAKE_PR_LIST_FAIL"] = "1"
		proc = _deliver(tmp, env)
		out = _out(proc)
		assert proc.returncode == 0, out
		assert "WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=degraded reason=pr_list_failed" in out
		assert "outcome=pr_opened" in out


def test_delivery_lint_failure_blocks_push_and_create() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {REPORT: "x\n"})
		(tmp / "trusted" / "scripts" / "lint_pr_body_auto_close.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
		proc = _deliver(tmp, env)
		out = _out(proc)
		assert proc.returncode == 1
		assert f"reason=pr_body_lint_failed branch={RUN_BRANCH}" in out
		assert "pr create" not in (tmp / "gh.log").read_text(encoding="utf-8")
		# The local rename was not pushed: the lint runs before any push.
		assert _ls_remote_file(tmp, env, RUN_BRANCH, REPORT)


def test_delivery_classifies_ruleset_rejection() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {REPORT: "x\n"})
		hook = tmp / "origin.git" / "hooks" / "pre-receive"
		hook.write_text("#!/bin/sh\necho 'error: GH013: Repository rule violations found' >&2\nexit 1\n", encoding="utf-8")
		hook.chmod(0o755)
		proc = _deliver(tmp, env)
		out = _out(proc)
		assert proc.returncode == 1
		assert f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=push_rejected_by_ruleset branch={RUN_BRANCH}" in out
		assert "pr create" not in (tmp / "gh.log").read_text(encoding="utf-8")


# --- commit step: purge + per-run branch push --------------------------------

OLD_PROCESSED = "analysis/workflow-optimization-2020-01-01.md"
OLD_UNPROCESSED = "analysis/workflow-optimization-2020-01-02.md"
OLD_IN_FLIGHT = "analysis/workflow-optimization-2020-01-03.md"


def _run_commit_step(tmp: Path, with_processing_report: bool) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str]:
	env = _env(tmp)
	files = {
		OLD_PROCESSED: "a\n",
		OLD_UNPROCESSED: "b\n",
		OLD_IN_FLIGHT: "c\n",
		"scripts/apply_analysis_on_main.sh": f"#!/usr/bin/env bash\necho {OLD_IN_FLIGHT}\n",
	}
	if with_processing_report:
		files["analysis/recommendation-processing-report.md"] = (
			"| workflow-optimization-2020-01-01.md | done |\n| workflow-optimization-2020-01-03.md | done |\n"
		)
	url, _seed = _origin(tmp, env, files)
	main_before = _git(tmp / "origin.git", env, "rev-parse", "main")
	work = tmp / "work"
	_git(tmp, env, "clone", "-q", url, str(work))
	_write(work, REPORT, "# new report\n")
	body = _step_run("Commit and push report").replace("${{ steps.analyze.outputs.report_file }}", REPORT)
	assert "${{" not in body
	env.update({
		"GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "main",
		"GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
		"GITHUB_OUTPUT": str(tmp / "github_output"),
		"REPORT_RETENTION_DAYS": "30",
	})
	proc = subprocess.run(["bash", "-c", body], cwd=work, env=env, capture_output=True, text=True, timeout=120)
	assert _git(tmp / "origin.git", env, "rev-parse", "main") == main_before, "main must not move"
	return proc, env, (tmp / "github_output").read_text(encoding="utf-8") if (tmp / "github_output").exists() else ""


def test_commit_step_purges_only_processed_docs_and_pushes_run_branch() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		proc, env, output = _run_commit_step(tmp, with_processing_report=True)
		out = _out(proc)
		assert proc.returncode == 0, out
		assert f"WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc={OLD_UNPROCESSED} reason=unprocessed" in out
		assert f"WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc={OLD_IN_FLIGHT} reason=in_flight_project" in out
		assert not _ls_remote_file(tmp, env, RUN_BRANCH, OLD_PROCESSED)
		assert _ls_remote_file(tmp, env, RUN_BRANCH, OLD_UNPROCESSED)
		assert _ls_remote_file(tmp, env, RUN_BRANCH, OLD_IN_FLIGHT)
		assert _ls_remote_file(tmp, env, RUN_BRANCH, REPORT)
		assert "committed=true" in output
		assert f"report_branch={RUN_BRANCH}" in output
		assert f"purged_docs={OLD_PROCESSED}\n" in output


def test_commit_step_skips_purge_without_processing_report() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		proc, env, output = _run_commit_step(tmp, with_processing_report=False)
		out = _out(proc)
		assert proc.returncode == 0, out
		assert "WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc=all reason=processing_report_missing" in out
		for doc in (OLD_PROCESSED, OLD_UNPROCESSED, OLD_IN_FLIGHT, REPORT):
			assert _ls_remote_file(tmp, env, RUN_BRANCH, doc), doc
		assert "purged_docs=\n" in output


# --- section step -------------------------------------------------------------

def _run_section_step(tmp: Path, report_branch: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
	env = _env(tmp)
	url, seed = _origin(tmp, env, {})
	_push_branch(seed, env, RUN_BRANCH, {REPORT: "# report\n"})
	work = tmp / "work"
	_git(tmp, env, "clone", "-q", "--branch", RUN_BRANCH, url, str(work))
	with (work / REPORT).open("a", encoding="utf-8") as handle:
		handle.write("\n## Deep audit\n")
	env.update({
		"GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "main",
		"REPORT_FILE": REPORT, "REPORT_BRANCH": report_branch,
	})
	body = _step_run("Commit and push deep audit section")
	assert "${{" not in body
	proc = subprocess.run(["bash", "-c", body], cwd=work, env=env, capture_output=True, text=True, timeout=120)
	return proc, env


def test_section_step_pushes_to_run_branch() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		proc, env = _run_section_step(tmp, RUN_BRANCH)
		assert proc.returncode == 0, _out(proc)
		text = _git(tmp / "origin.git", env, "show", f"{RUN_BRANCH}:{REPORT}")
		assert "## Deep audit" in text
		assert not _ls_remote_file(tmp, env, "main", REPORT)


def test_section_step_without_report_branch_pushes_nothing() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		proc, env = _run_section_step(tmp, "")
		out = _out(proc)
		assert proc.returncode == 0, out
		assert "WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=skipped reason=no_report_branch job=deep-audit" in out
		assert "## Deep audit" not in _git(tmp / "origin.git", env, "show", f"{RUN_BRANCH}:{REPORT}")


# --- review follow-ups: credentials, default branch, paths, section results --

GIT_HELPER_ENV = (
	"GIT_CONFIG_COUNT: '1'",
	"GIT_CONFIG_KEY_0: credential.helper",
	"GIT_CONFIG_VALUE_0: '!f() { printf \"username=x-access-token\\npassword=%s\\n\" \"${GH_TOKEN}\"; }; f'",
)


def test_pushing_jobs_do_not_persist_credentials_and_use_one_shot_helper() -> None:
	for job_id in ("analyze-commit-notify", "deep-audit", "api-redundancy", "deliver-report"):
		job = _job_block(job_id)
		checkout = job.split("      - name: Checkout repository\n", 1)[1].split("\n      - name: ", 1)[0]
		assert "persist-credentials: false" in checkout, job_id
	wf = _wf()
	sync_marker = "      - name: Sync latest report from target branch\n"
	blocks = [_step_block(name) for name in COMMIT_STEPS]
	blocks += [b.split("\n      - name: ", 1)[0] for b in wf.split(sync_marker)[1:]]
	assert len(blocks) == 5
	for block in blocks:
		env = block.split("        run: |\n", 1)[0]
		assert "GH_TOKEN: ${{ secrets.GH_PAT || github.token }}" in env
		for line in GIT_HELPER_ENV:
			assert line in env, line
	deliver = _job_block("deliver-report")
	assert "DEFAULT_BRANCH: ${{ github.event.repository.default_branch || '' }}" in deliver
	assert "DEEP_AUDIT_RESULT: ${{ needs.deep-audit.result }}" in deliver
	assert "API_REDUNDANCY_RESULT: ${{ needs.api-redundancy.result }}" in deliver


def test_delivery_fails_closed_on_non_default_ref() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {})
		proc = _deliver(tmp, dict(env, DEFAULT_BRANCH="trunk"))
		out = _out(proc)
		assert proc.returncode == 1
		assert "WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=run_context_invalid" in out
		assert (tmp / "gh.log").read_text(encoding="utf-8") == ""
		proc = _deliver(tmp, dict(env, DEFAULT_BRANCH="main"))
		assert proc.returncode == 0, _out(proc)
		assert "outcome=pr_opened" in proc.stdout


def test_delivery_rejects_run_branch_with_unexpected_paths() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {})
		seed = tmp / "seed"
		_git(seed, env, "checkout", "-q", RUN_BRANCH)
		_write(seed, "scripts/evil.sh", "echo pwned\n")
		_git(seed, env, "add", "-A")
		_git(seed, env, "commit", "-q", "-m", "stray path")
		_git(seed, env, "push", "-q", "origin", f"HEAD:refs/heads/{RUN_BRANCH}")
		proc = _deliver(tmp, env)
		out = _out(proc)
		assert proc.returncode == 1
		assert f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=unexpected_paths branch={RUN_BRANCH}" in out
		assert "pr create" not in (tmp / "gh.log").read_text(encoding="utf-8")


def test_delivery_rejects_symlink_at_analysis_doc_path() -> None:
	# The path check must also reject a non-regular file whose name matches.
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {})
		seed = tmp / "seed"
		_git(seed, env, "checkout", "-q", RUN_BRANCH)
		(seed / REPORT).unlink()
		(seed / REPORT).symlink_to("../README.md")
		_git(seed, env, "add", "-A")
		_git(seed, env, "commit", "-q", "-m", "symlinked report")
		_git(seed, env, "push", "-q", "origin", f"HEAD:refs/heads/{RUN_BRANCH}")
		proc = _deliver(tmp, env)
		out = _out(proc)
		assert proc.returncode == 1, out
		assert "non-regular file (mode 120000)" in out
		assert f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=unexpected_paths branch={RUN_BRANCH}" in out
		assert "pr create" not in (tmp / "gh.log").read_text(encoding="utf-8")


def test_delivery_body_does_not_claim_sections_of_failed_jobs() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		env = _delivery_sandbox(tmp, {})
		env.update({
			"AUDIT_STATUS": "completed", "DEEP_AUDIT_RESULT": "failure",
			"API_REDUNDANCY_STATUS": "completed", "API_REDUNDANCY_RESULT": "success",
		})
		proc = _deliver(tmp, env)
		assert proc.returncode == 0, _out(proc)
		body = (tmp / "body.md").read_text(encoding="utf-8")
		assert "Deep audit section: not confirmed (job failure)" in body
		assert "API redundancy section: completed" in body


def main() -> int:
	for name, func in sorted(globals().items()):
		if name.startswith("test_") and callable(func):
			func()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
