#!/usr/bin/env python3
"""Tests for gh_read (scripts/gh_helpers.sh) and its release-gate twin
gh_api_safe_job_token_read (scripts/comprehensive_test_and_release_gh_api.sh).

Issue #9888: the polling loops read with the job's GITHUB_TOKEN
(GH_READ_TOKEN) instead of GH_PAT, fall back to the PAT once on
401/403/404 or a rate limit, never send a write through the job token,
and keep today's behaviour when GH_READ_TOKEN is unset or the kill switch
GH_READ_TOKEN_ENABLED is off. A fake ``gh`` on PATH records the token each
call used; the job token's answer comes from FAKE_JOB_* variables and the
PAT always answers ``pat-output``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
GH_HELPERS = SCRIPTS / "gh_helpers.sh"
RELEASE_GH_API = SCRIPTS / "comprehensive_test_and_release_gh_api.sh"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

JOB_TOKEN = "job-secret-token-9888"
PAT = "pat-secret-token-9888"
REPO = "owner/repo"

FAKE_GH = r"""#!/usr/bin/env bash
printf 'token=%s args=%s\n' "${GH_TOKEN:-}" "$*" >> "${FAKE_GH_CALLS}"
if [ "${GH_TOKEN:-}" = "${FAKE_JOB_TOKEN}" ]; then
	printf '%s' "${FAKE_JOB_STDOUT:-job-output}"
	if [ -n "${FAKE_JOB_STDERR:-}" ]; then
		printf '%s\n' "${FAKE_JOB_STDERR}" >&2
	fi
	exit "${FAKE_JOB_RC:-0}"
fi
printf '%s' "${FAKE_PAT_STDOUT:-pat-output}"
if [ -n "${FAKE_PAT_STDERR:-}" ]; then
	printf '%s\n' "${FAKE_PAT_STDERR}" >&2
fi
exit "${FAKE_PAT_RC:-0}"
"""

FALLBACKS = (
	("gh: Bad credentials (HTTP 401)", "401", True),
	("gh: Resource not accessible by integration (HTTP 403)", "403", True),
	("gh: Not Found (HTTP 404)", "404", False),
	("gh: API rate limit exceeded for installation ID 1. (HTTP 403)", "rate_limit", False),
)

WRITE_COMMANDS = (
	"api -X POST repos/owner/repo/issues/1/comments",
	"api -XPATCH repos/owner/repo/issues/1",
	"api --method PUT repos/owner/repo/pulls/1/merge",
	"api --method=delete repos/owner/repo/issues/comments/1",
	"api repos/owner/repo/issues/1/comments -f body=x",
	"api repos/owner/repo/issues/1/comments --raw-field body=x",
	"api --input body.json repos/owner/repo/issues/1/comments",
	"api graphql -X GET -f query=x",
	"api repos/owner/repo/issues -X",
	"pr merge 1 -R owner/repo",
	"issue comment 1 --body x -R owner/repo",
	"workflow run ci.yml -R owner/repo",
	"run cancel 1 -R owner/repo",
	"label create x -R owner/repo",
)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	fake = bin_dir / "gh"
	fake.write_text(FAKE_GH, encoding="utf-8")
	fake.chmod(0o755)
	runner_temp = tmp_path / "runner_temp"
	runner_temp.mkdir(exist_ok=True)
	env = {
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"HOME": str(tmp_path),
		"TMPDIR": str(tmp_path),
		"RUNNER_TEMP": str(runner_temp),
		"GITHUB_RUN_ID": "9888",
		"GITHUB_RUN_ATTEMPT": "1",
		"GITHUB_JOB": "poll",
		"GITHUB_REPOSITORY": REPO,
		"GH_TOKEN": PAT,
		"GH_READ_TOKEN": JOB_TOKEN,
		"FAKE_JOB_TOKEN": JOB_TOKEN,
		"FAKE_GH_CALLS": str(tmp_path / "calls.log"),
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	env.update(extra)
	return env


def _run(
	tmp_path: Path,
	body: str,
	env: dict[str, str],
	helper: Path = GH_HELPERS,
	strict: bool = False,
) -> subprocess.CompletedProcess[str]:
	script = tmp_path / "poller.sh"
	# strict: the callers' order, set -euo pipefail before the helper is sourced.
	prefix = "set -euo pipefail\n" if strict else ""
	script.write_text(f"{prefix}source '{helper}'\n{body}\n", encoding="utf-8")
	return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60, check=False)


def _calls(env: dict[str, str]) -> list[str]:
	path = Path(env["FAKE_GH_CALLS"])
	if not path.exists():
		return []
	return path.read_text(encoding="utf-8").splitlines()


def _tokens(env: dict[str, str]) -> list[str]:
	return [line.split(" ", 1)[0].removeprefix("token=") for line in _calls(env)]


# --- gh_read -------------------------------------------------------------------


def test_read_uses_the_job_token(tmp_path: Path) -> None:
	env = _env(tmp_path)
	result = _run(tmp_path, f'gh_read --same-repo-only api "repos/{REPO}/actions/runs/1" --jq .status', env)
	assert result.returncode == 0, result.stderr
	assert result.stdout == "job-output"
	assert _tokens(env) == [JOB_TOKEN]
	assert "GH_READ_FALLBACK" not in result.stderr


@pytest.mark.parametrize("stderr,reason,latches", FALLBACKS)
def test_fallback_once_to_the_pat(tmp_path: Path, stderr: str, reason: str, latches: bool) -> None:
	env = _env(tmp_path, FAKE_JOB_STDERR=stderr, FAKE_JOB_RC="1", FAKE_JOB_STDOUT="partial-page")
	result = _run(tmp_path, f'gh_read --same-repo-only api --paginate "repos/{REPO}/issues/1/comments"', env)
	assert result.returncode == 0, result.stderr
	# The job token's output is dropped; only the PAT's answer reaches stdout.
	assert result.stdout == "pat-output"
	assert _tokens(env) == [JOB_TOKEN, PAT]
	assert f"GH_READ_FALLBACK reason={reason} caller=poller.sh" in result.stderr
	assert stderr not in result.stderr
	assert JOB_TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize("stderr,reason,latches", FALLBACKS)
def test_latch_only_on_401_and_permission_403(tmp_path: Path, stderr: str, reason: str, latches: bool) -> None:
	env = _env(tmp_path, FAKE_JOB_STDERR=stderr, FAKE_JOB_RC="1")
	body = f'gh_read api "repos/{REPO}/pulls/1"\necho\ngh_read api "repos/{REPO}/pulls/1"'
	result = _run(tmp_path, body, env)
	assert result.returncode == 0, result.stderr
	if latches:
		assert _tokens(env) == [JOB_TOKEN, PAT, PAT]
		assert result.stderr.count("GH_READ_FALLBACK") == 1
		assert list((tmp_path / "runner_temp").glob("gh_read_refused_9888_1_poll"))
	else:
		assert _tokens(env) == [JOB_TOKEN, PAT, JOB_TOKEN, PAT]
		assert result.stderr.count("GH_READ_FALLBACK") == 2
		assert not list((tmp_path / "runner_temp").glob("gh_read_refused_*"))


def test_other_failures_return_the_job_token_error_without_fallback(tmp_path: Path) -> None:
	env = _env(tmp_path, FAKE_JOB_STDERR="gh: Server Error (HTTP 502)", FAKE_JOB_RC="1")
	result = _run(tmp_path, f'gh_read api "repos/{REPO}/pulls/1"', env)
	assert result.returncode == 1
	assert _tokens(env) == [JOB_TOKEN]
	assert "HTTP 502" in result.stderr
	assert "GH_READ_FALLBACK" not in result.stderr


@pytest.mark.parametrize(
	"overrides",
	(
		{"GH_READ_TOKEN": ""},
		{"GH_READ_TOKEN_ENABLED": "false"},
		{"GH_READ_TOKEN_ENABLED": "OFF"},
		{"GH_READ_TOKEN_ENABLED": "0"},
	),
)
def test_plain_gh_when_unset_or_disabled(tmp_path: Path, overrides: dict[str, str]) -> None:
	env = _env(tmp_path, **overrides)
	result = _run(tmp_path, f'gh_read --same-repo-only api "repos/{REPO}/pulls/1"', env)
	assert result.returncode == 0, result.stderr
	assert result.stdout == "pat-output"
	assert _tokens(env) == [PAT]


def test_plain_gh_when_read_token_is_missing_from_env(tmp_path: Path) -> None:
	env = _env(tmp_path)
	del env["GH_READ_TOKEN"]
	result = _run(tmp_path, f'gh_read api "repos/{REPO}/pulls/1"', env)
	assert result.returncode == 0
	assert _tokens(env) == [PAT]


@pytest.mark.parametrize("command", WRITE_COMMANDS)
@pytest.mark.parametrize("enabled", ("true", "false"))
def test_write_verbs_are_refused(tmp_path: Path, command: str, enabled: str) -> None:
	env = _env(tmp_path, GH_READ_TOKEN_ENABLED=enabled)
	result = _run(tmp_path, f"gh_read {command}", env)
	assert result.returncode == 2
	assert _calls(env) == []
	assert "gh_read: refused non-read command" in result.stderr
	assert "body=x" not in result.stderr


@pytest.mark.parametrize(
	"command",
	(
		f"api -X GET repos/{REPO}/issues -F state=open -F per_page=100",
		f"api --method=get repos/{REPO}/pulls/1",
		f"api -XHEAD repos/{REPO}/pulls/1",
		f"api /repos/{REPO}/pulls/1 --jq .state",
		f"run view 1 -R {REPO}",
		f"pr view 1 --repo {REPO}",
		f"issue list --repo={REPO}",
	),
)
def test_reads_go_through_the_job_token(tmp_path: Path, command: str) -> None:
	env = _env(tmp_path)
	result = _run(tmp_path, f"gh_read --same-repo-only {command}", env)
	assert result.returncode == 0, result.stderr
	assert _tokens(env) == [JOB_TOKEN]


@pytest.mark.parametrize(
	"command",
	(
		"api repos/other/private-repo/actions/runs/1",
		"api user",
		"run view 1 -R other/private-repo",
		"run view 1",
	),
)
def test_same_repo_only_sends_other_targets_to_the_pat(tmp_path: Path, command: str) -> None:
	env = _env(tmp_path)
	result = _run(tmp_path, f"gh_read --same-repo-only {command}", env)
	assert result.returncode == 0, result.stderr
	assert _tokens(env) == [PAT]
	assert "GH_READ_FALLBACK" not in result.stderr


def test_gh_retry_wrapped_fallback_names_the_calling_script(tmp_path: Path) -> None:
	env = _env(tmp_path, FAKE_JOB_STDERR="gh: Not Found (HTTP 404)", FAKE_JOB_RC="1")
	result = _run(tmp_path, f'out="$(gh_retry gh_read --same-repo-only api "repos/{REPO}/pulls/1")"\nprintf "%s" "$out"', env)
	assert result.returncode == 0, result.stderr
	assert result.stdout == "pat-output"
	assert "GH_READ_FALLBACK reason=404 caller=poller.sh" in result.stderr
	assert _tokens(env) == [JOB_TOKEN, PAT]


def test_token_is_never_printed_under_xtrace(tmp_path: Path) -> None:
	env = _env(tmp_path, FAKE_JOB_STDERR="gh: Resource not accessible by integration (HTTP 403)", FAKE_JOB_RC="1")
	result = _run(tmp_path, f'set -x\ngh_read --same-repo-only api "repos/{REPO}/pulls/1"\ngh_read api "repos/{REPO}/pulls/2"\necho "x-$-"', env)
	assert result.returncode == 0
	assert JOB_TOKEN not in result.stdout
	assert JOB_TOKEN not in result.stderr
	# xtrace is restored for the caller.
	assert "x-" in result.stdout and "x" in result.stdout.rsplit("x-", 1)[1]


def test_gh_review_pr_state_reads_through_gh_read(tmp_path: Path) -> None:
	env = _env(tmp_path, FAKE_JOB_STDOUT="open")
	env["RUNNER_TEMP"] = ""
	result = _run(tmp_path, f"gh_review_pr_state {REPO} 7", env)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "open"
	assert _tokens(env) == [JOB_TOKEN]



def test_gh_review_pr_state_keeps_only_the_fallback_line_on_stderr(tmp_path: Path) -> None:
	env = _env(tmp_path, FAKE_JOB_STDERR="gh: Not Found (HTTP 404)", FAKE_JOB_RC="1")
	env["RUNNER_TEMP"] = ""
	result = _run(tmp_path, f"gh_review_pr_state {REPO} 7", env)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "open"
	assert _tokens(env) == [JOB_TOKEN, PAT]
	# The caller drops gh's own stderr but keeps the contractual line.
	assert "GH_READ_FALLBACK reason=404 caller=poller.sh" in result.stderr
	assert "HTTP 404" not in result.stderr


def test_fallback_line_survives_a_caller_filter_when_the_pat_also_fails(tmp_path: Path) -> None:
	# The heal queue / gh_review_pr_state filter keeps only lines starting with
	# GH_READ_FALLBACK; gh_retry must replay the line from a failed attempt too.
	env = _env(
		tmp_path,
		FAKE_JOB_STDERR="gh: Not Found (HTTP 404)",
		FAKE_JOB_RC="1",
		FAKE_PAT_STDERR="gh: Not Found (HTTP 404)",
		FAKE_PAT_RC="1",
	)
	body = (
		f'if gh_retry gh_read --same-repo-only api --method GET "repos/{REPO}/pulls/1" > "{tmp_path}/out.json" '
		"2> >(grep -a '^GH_READ_FALLBACK ' >&2 || true); then echo ok; else echo failed; fi\nwait"
	)
	result = _run(tmp_path, body, env)
	assert result.stdout.strip() == "failed"
	assert _tokens(env) == [JOB_TOKEN, PAT]
	assert re.search(r"^GH_READ_FALLBACK reason=404 caller=poller\.sh$", result.stderr, re.MULTILINE), result.stderr
	assert "HTTP 404" not in result.stderr
	assert JOB_TOKEN not in result.stdout + result.stderr


# --- gh_api_safe_job_token_read / gh_api_safe (release-gate twin) ---------------


def test_twin_reads_with_the_job_token(tmp_path: Path) -> None:
	env = _env(tmp_path)
	result = _run(tmp_path, f'gh_api_safe_job_token_read "repos/{REPO}/actions/runs/1" --jq .status', env, RELEASE_GH_API)
	assert result.returncode == 0, result.stderr
	assert result.stdout == "job-output"
	assert _tokens(env) == [JOB_TOKEN]


@pytest.mark.parametrize("stderr,reason,latches", FALLBACKS)
def test_twin_parity_fallback_and_latch(tmp_path: Path, stderr: str, reason: str, latches: bool) -> None:
	env = _env(tmp_path, FAKE_JOB_STDERR=stderr, FAKE_JOB_RC="1")
	body = (
		f'gh_api_safe_job_token_read "repos/{REPO}/pulls/1"; echo "rc=$?"\n'
		f'gh_api_safe_quiet_print_read "repos/{REPO}/pulls/1"; echo " rc2=$?"'
	)
	result = _run(tmp_path, body, env, RELEASE_GH_API)
	assert "rc=3" in result.stdout
	assert "pat-output rc2=0" in result.stdout
	assert f"GH_READ_FALLBACK reason={reason} caller=poller.sh" in result.stderr
	if latches:
		assert _tokens(env) == [JOB_TOKEN, PAT]
	else:
		assert _tokens(env) == [JOB_TOKEN, JOB_TOKEN, PAT]
	assert JOB_TOKEN not in result.stdout + result.stderr


def test_twin_other_failure_falls_through_to_the_pat_loop(tmp_path: Path) -> None:
	env = _env(tmp_path, FAKE_JOB_STDERR="gh: Server Error (HTTP 500)", FAKE_JOB_RC="1")
	result = _run(tmp_path, f'gh_api_safe_job_token_read "repos/{REPO}/pulls/1"; echo "rc=$?"\ngh_api_safe_quiet_print_read "repos/{REPO}/pulls/1"', env, RELEASE_GH_API)
	assert "rc=1" in result.stdout
	assert result.stdout.endswith("pat-output")
	assert "GH_READ_FALLBACK" not in result.stderr
	assert _tokens(env) == [JOB_TOKEN, JOB_TOKEN, PAT]


@pytest.mark.parametrize(
	"overrides",
	({"GH_READ_TOKEN": ""}, {"GH_READ_TOKEN_ENABLED": "false"}, {"GITHUB_REPOSITORY": "other/private-repo"}),
)
def test_twin_plain_mode_and_cross_repo_use_the_pat_only(tmp_path: Path, overrides: dict[str, str]) -> None:
	env = _env(tmp_path, **overrides)
	result = _run(tmp_path, f'gh_api_safe_quiet_print_read "repos/{REPO}/pulls/1"', env, RELEASE_GH_API)
	assert result.returncode == 0, result.stderr
	assert result.stdout == "pat-output"
	assert _tokens(env) == [PAT]


def test_gh_api_safe_default_stays_on_the_pat(tmp_path: Path) -> None:
	env = _env(tmp_path)
	result = _run(tmp_path, f'gh_api_safe_quiet_print "repos/{REPO}/pulls/1"', env, RELEASE_GH_API)
	assert result.stdout == "pat-output"
	assert _tokens(env) == [PAT]


@pytest.mark.parametrize(
	"args",
	(
		f"-X POST repos/{REPO}/actions/runs/1/cancel",
		f"repos/{REPO}/issues/1/comments -f body=x",
		f"--input body.json repos/{REPO}/issues/1",
		"graphql",
	),
)
def test_twin_refuses_writes(tmp_path: Path, args: str) -> None:
	env = _env(tmp_path)
	result = _run(tmp_path, f"gh_api_safe_job_token_read {args}; echo \"rc=$?\"", env, RELEASE_GH_API)
	assert "rc=2" in result.stdout
	assert _calls(env) == []
	# gh_api_safe never sends a write through the job token, even opted in.
	opted_in_dir = tmp_path / "opted_in"
	opted_in_dir.mkdir()
	opted_in_env = _env(opted_in_dir)
	_run(opted_in_dir, f"GH_API_SAFE_USE_READ_TOKEN=1 gh_api_safe_quiet_print -X POST repos/{REPO}/actions/runs/1/cancel", opted_in_env, RELEASE_GH_API)
	assert _tokens(opted_in_env) == [PAT]


def test_twin_token_is_never_printed_under_xtrace(tmp_path: Path) -> None:
	env = _env(tmp_path, FAKE_JOB_STDERR="gh: Bad credentials (HTTP 401)", FAKE_JOB_RC="1")
	result = _run(tmp_path, f'set -x\ngh_api_safe_quiet_print_read "repos/{REPO}/pulls/1"', env, RELEASE_GH_API)
	assert JOB_TOKEN not in result.stdout + result.stderr


# --- call sites and workflows ---------------------------------------------------

READ_HELPER_CALL_RE = re.compile(r"\b(gh_read|gh_api_safe_quiet_print_read|gh_api_safe_job_token_read)\b(?!\(\))(.*)")
WRITE_MARKERS_RE = re.compile(
	r"-X\s*(POST|PATCH|PUT|DELETE)|--method[ =](POST|PATCH|PUT|DELETE)|--input|\s-[fF]\s|--raw-field|--field"
	r"|\bpr merge\b|\bissue comment\b|\bworkflow run\b|\brun cancel\b|/cancel\b|force-cancel",
	re.IGNORECASE,
)


def test_no_write_verb_goes_through_a_read_helper() -> None:
	offenders = []
	for path in sorted(SCRIPTS.glob("*.sh")):
		for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
			stripped = line.strip()
			if stripped.startswith("#") or stripped.startswith("type "):
				continue
			match = READ_HELPER_CALL_RE.search(line)
			if match and WRITE_MARKERS_RE.search(match.group(2)):
				offenders.append(f"{path.name}:{number}: {stripped}")
	assert offenders == []


def test_polling_call_sites_use_the_read_helpers() -> None:
	promote = (SCRIPTS / "promote_main_cycle.sh").read_text(encoding="utf-8")
	# Issue #10101: both gate-run reads go through promote_cycle_gate_runs_read
	# (the job token once through gh_read, then the bounded PAT loop).
	assert promote.count('promote_cycle_gate_runs_read "repos/${GITHUB_REPOSITORY}/actions/workflows/${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}/runs?') == 2
	gate_runs_read = _promote_gate_runs_read_function()
	assert 'gh_read --same-repo-only api "$1"' in gate_runs_read
	assert 'gh_retry gh api "$1"' in gate_runs_read
	# The GH_READ_FALLBACK line must reach the job log: no gh_read call in the
	# promote cycle may send its stderr to /dev/null.
	assert not [line for line in promote.splitlines() if "gh_read --same-repo-only" in line and "2>/dev/null" in line]
	assert 'gh_retry gh workflow run "${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}"' in promote
	queue = (SCRIPTS / "workflow_failure_heal_queue.sh").read_text(encoding="utf-8")
	assert queue.count("gh_retry gh_read --same-repo-only api --method GET") == 2
	assert 'gh_retry gh api --method POST "repos/${SELF_REPO}/issues/${QUEUE_ISSUE}/comments"' in queue
	watch = (SCRIPTS / "dispatch_and_watch_workflow_run.sh").read_text(encoding="utf-8")
	watch_code = "\n".join(line for line in watch.splitlines() if not line.lstrip().startswith("#"))
	assert watch_code.count("gh_api_safe_quiet_print_read") == 2
	wait_review = (SCRIPTS / "stable_e2e_step_wait_review.sh").read_text(encoding="utf-8")
	assert 'GH_API_SAFE_USE_READ_TOKEN="${GH_API_SAFE_USE_READ_TOKEN:-1}"' in wait_review
	cancel = (SCRIPTS / "stable_orchestrate_cancel_dispatched_run.sh").read_text(encoding="utf-8")
	assert 'gh_api_safe_job_token_read "repos/${TEST_REPO}/actions/runs/${1}"' in cancel
	helpers = GH_HELPERS.read_text(encoding="utf-8")
	assert helpers.count('gh_retry gh_read --same-repo-only api "repos/${repo}/pulls/${number}" --jq .state') == 2


def _step(text: str, name_line: str) -> str:
	start = text.index(name_line)
	end = text.find("\n      - name:", start + 1)
	return text[start:end if end != -1 else len(text)]


TOKEN_LINE = "GH_READ_TOKEN: ${{ vars.GH_READ_TOKEN_ENABLED != 'false' && github.token || '' }}"


@pytest.mark.parametrize(
	"workflow,step",
	(
		("promote-main-to-stable.yml", "      - name: Run the promote cycle\n"),
		("test-and-mark-stable.yml", '      - name: "Phase 4: Wait for review & autofix to complete"\n'),
		("test-and-mark-stable.yml", "      - name: Dispatch & snapshot — workflow-log-analysis\n"),
		("test-and-mark-stable.yml", "      - name: Dispatch & watch — validation-refresh\n"),
		("test-and-mark-stable.yml", "      - name: Dispatch & watch — update_workflows (allow_workflow_edits=false)\n"),
		("test-and-mark-stable.yml", "      - name: Dispatch & watch — internal-memory-maintenance\n"),
		("test-and-mark-stable.yml", "      - name: Dispatch internal-orchestrate.yml with multi-issue project\n"),
		("test-and-mark-stable.yml", "      - name: Cleanup orchestrate test issues\n"),
		("test-and-mark-stable.yml", "      - name: Dispatch internal-validate.yml standalone\n"),
		("review_autofix.yml", '      - name: "Preflight: Verify required files before reviewer invocation"\n'),
		("review_autofix.yml", "      - name: Run reviewer models\n"),
		("review_autofix.yml", "      - name: Apply fixes with editor model\n"),
	),
)
def test_polling_steps_export_the_job_token(workflow: str, step: str) -> None:
	text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
	assert text.count(step) == 1
	assert TOKEN_LINE in _step(text, step)


def test_heal_intake_jobs_export_the_job_token() -> None:
	text = (WORKFLOWS / "workflow-failure-heal-intake.yml").read_text(encoding="utf-8")
	intake = text[text.index("\n  intake:\n"):text.index("\n  recovery-candidates:\n")]
	scan = text[text.index("\n  recovery-scan:\n"):text.index("\n  heal-verify:\n")]
	assert TOKEN_LINE in intake
	assert TOKEN_LINE in scan
	assert text.count(TOKEN_LINE) == 2


def test_release_gate_adds_only_read_scopes() -> None:
	text = (WORKFLOWS / "test-and-mark-stable.yml").read_text(encoding="utf-8")
	block = text[text.index("\npermissions:\n") + 1:text.index("\njobs:\n")]
	scopes = dict(
		line.strip().split(": ", 1)
		for line in block.splitlines()[1:]
		if line.strip() and not line.strip().startswith("#")
	)
	assert scopes == {"contents": "write", "actions": "read", "pull-requests": "read", "issues": "read"}


# --- issue #10101: a job-token failure never stops a call site -----------------
#
# Each case fails the job-token call and lets the PAT answer. Every call site
# runs under set -euo pipefail and must exit 0, use the PAT output, and never
# print a token value.

JOB_FAILURES = (
	pytest.param("gh: Resource not accessible by integration (HTTP 403)", id="http_403"),
	pytest.param("gh: API rate limit exceeded for installation ID 1. (HTTP 403)", id="rate_limit"),
	pytest.param("gh: HTTP 502: Bad Gateway (https://api.github.com/repos/owner/repo)", id="http_502"),
	pytest.param("error connecting to api.github.com", id="non_http"),
)

# Every test-and-mark-stable.yml step that exports GH_READ_TOKEN, and the
# script whose read helpers it reaches.
RELEASE_GATE_READ_STEPS = {
	'      - name: "Phase 4: Wait for review & autofix to complete"\n': "stable_e2e_step_wait_review.sh",
	"      - name: Dispatch & snapshot — workflow-log-analysis\n": "dispatch_and_watch_workflow_run.sh",
	"      - name: Dispatch & watch — validation-refresh\n": "dispatch_and_watch_workflow_run.sh",
	"      - name: Dispatch & watch — update_workflows (allow_workflow_edits=false)\n": "dispatch_and_watch_workflow_run.sh",
	"      - name: Dispatch & watch — internal-memory-maintenance\n": "dispatch_and_watch_workflow_run.sh",
	"      - name: Dispatch internal-orchestrate.yml with multi-issue project\n": "dispatch_and_watch_workflow_run.sh",
	"      - name: Cleanup orchestrate test issues\n": "stable_orchestrate_cancel_dispatched_run.sh",
	"      - name: Dispatch internal-validate.yml standalone\n": "dispatch_and_watch_workflow_run.sh",
}

# Issue #10101: every job-token read call those steps reach, per script and
# helper. Comment lines, `type <helper>` probes and function definitions are
# not counted. In stable_e2e_step_wait_review.sh every gh_api_safe_quiet_print
# runs after the GH_API_SAFE_USE_READ_TOKEN opt-in, so each one tries the job
# token first. The two plain gh_api_safe_quiet_print calls in
# dispatch_and_watch_workflow_run.sh stay on the PAT (no opt-in there) and are
# not listed. A new call site changes a count and must get a fallback test.
RELEASE_GATE_READ_CALL_SITES = {
	"stable_e2e_step_wait_review.sh": {"gh_api_safe_quiet_print": 14},
	"dispatch_and_watch_workflow_run.sh": {"gh_api_safe_quiet_print_read": 2},
	"stable_orchestrate_cancel_dispatched_run.sh": {"gh_api_safe_job_token_read": 1},
	"promote_main_cycle.sh": {"gh_read": 1, "promote_cycle_gate_runs_read": 2},
}
# Calls of the same helpers that stay on the PAT; listed so the counts above
# can be exact.
RELEASE_GATE_PAT_ONLY_CALLS = {
	"dispatch_and_watch_workflow_run.sh": {"gh_api_safe_quiet_print": 2},
}
READ_HELPERS = (
	"gh_api_safe_job_token_read",
	"gh_api_safe_quiet_print_read",
	"gh_api_safe_quiet_print",
	"gh_read",
	"promote_cycle_gate_runs_read",
)


def _read_helper_calls(text: str, helper: str) -> list[int]:
	pattern = re.compile(r"(?<![\w$-])(?<!type )" + re.escape(helper) + r"(?![\w(-])")
	return [
		number
		for number, line in enumerate(text.splitlines(), start=1)
		if not line.lstrip().startswith("#")
		for _ in pattern.finditer(line)
	]


WAIT_REVIEW_OPT_IN = 'GH_API_SAFE_USE_READ_TOKEN="${GH_API_SAFE_USE_READ_TOKEN:-1}"'
DISPATCH_WATCH_REGISTRATION = 'NEW_ID="$(gh_api_safe_quiet_print_read "${REGISTRATION_QUERY}" --jq "${REGISTRATION_JQ}" || echo "")"'
DISPATCH_WATCH_STATUS = (
	'RUN_JSON="$(gh_api_safe_quiet_print_read "repos/${TARGET_REPO}/actions/runs/${NEW_ID}" --jq \'{status, conclusion}\' || echo "")"'
)
PROMOTE_IDLE_READ = (
	'gate_runs_json="$(promote_cycle_gate_runs_read "repos/${GITHUB_REPOSITORY}/actions/workflows/'
	'${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}/runs?per_page=30" 2> >(grep -a \'^GH_READ_FALLBACK \' >&2 || true) || echo "")"'
)
PROMOTE_WAIT_READ = (
	'runs_json="$(promote_cycle_gate_runs_read "repos/${GITHUB_REPOSITORY}/actions/workflows/'
	'${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=50")" || continue'
)


def _promote_gate_runs_read_function() -> str:
	text = (SCRIPTS / "promote_main_cycle.sh").read_text(encoding="utf-8")
	match = re.search(r"^promote_cycle_gate_runs_read\(\)\n\{\n.*?^\}\n", text, re.MULTILINE | re.DOTALL)
	assert match, "promote_cycle_gate_runs_read() is missing from scripts/promote_main_cycle.sh"
	return match.group(0)


def _assert_pat_fallback(result: subprocess.CompletedProcess[str], env: dict[str, str]) -> None:
	assert result.returncode == 0, result.stderr
	assert "rc=0" in result.stdout, result.stdout + result.stderr
	assert "job-output" not in result.stdout
	for token in (JOB_TOKEN, PAT):
		assert token not in result.stdout
		assert token not in result.stderr
	tokens = _tokens(env)
	assert tokens and tokens[0] == JOB_TOKEN and tokens[-1] == PAT, tokens


def test_release_gate_read_token_step_inventory() -> None:
	text = (WORKFLOWS / "test-and-mark-stable.yml").read_text(encoding="utf-8")
	# A new step that exports the job token must be added to the inventory
	# (and get a fallback test) before CI passes.
	assert text.count(TOKEN_LINE) == len(RELEASE_GATE_READ_STEPS) == 8
	for step, script in RELEASE_GATE_READ_STEPS.items():
		assert text.count(step) == 1, step
		body = _step(text, step)
		assert TOKEN_LINE in body, step
		assert f"scripts/{script}" in body, (step, script)
		assert (SCRIPTS / script).is_file(), script


def test_release_gate_read_call_site_inventory() -> None:
	assert set(RELEASE_GATE_READ_CALL_SITES) == set(RELEASE_GATE_READ_STEPS.values()) | {"promote_main_cycle.sh"}
	for script, expected in RELEASE_GATE_READ_CALL_SITES.items():
		text = (SCRIPTS / script).read_text(encoding="utf-8")
		found = {helper: len(_read_helper_calls(text, helper)) for helper in READ_HELPERS}
		listed = {**expected, **RELEASE_GATE_PAT_ONLY_CALLS.get(script, {})}
		assert {helper: count for helper, count in found.items() if count} == listed, (script, found)
	wait_review = (SCRIPTS / "stable_e2e_step_wait_review.sh").read_text(encoding="utf-8")
	opt_in_line = wait_review[: wait_review.index(WAIT_REVIEW_OPT_IN)].count("\n") + 1
	assert min(_read_helper_calls(wait_review, "gh_api_safe_quiet_print")) > opt_in_line
	watch = (SCRIPTS / "dispatch_and_watch_workflow_run.sh").read_text(encoding="utf-8")
	assert "GH_API_SAFE_USE_READ_TOKEN" not in watch


@pytest.mark.parametrize("job_stderr", JOB_FAILURES)
def test_wait_review_reads_fall_back_to_the_pat(tmp_path: Path, job_stderr: str) -> None:
	wait_review = (SCRIPTS / "stable_e2e_step_wait_review.sh").read_text(encoding="utf-8")
	assert ". ./scripts/comprehensive_test_and_release_gh_api.sh" in wait_review
	assert WAIT_REVIEW_OPT_IN in wait_review
	assert wait_review.index(WAIT_REVIEW_OPT_IN) < wait_review.index("gh_api_safe_quiet_print ")
	env = _env(tmp_path, FAKE_JOB_STDERR=job_stderr, FAKE_JOB_RC="1", FAKE_JOB_STDOUT="job-output")
	log_file = tmp_path / "job.log"
	body = (
		f"{WAIT_REVIEW_OPT_IN}\n"
		f'x=$(gh_api_safe_quiet_print "repos/{REPO}/actions/runs/1/jobs?per_page=10" || echo "")\n'
		'printf "jobs=%s\\n" "$x"\n'
		"if [ -z \"$x\" ]; then\n"
		"\techo log_skipped\n"
		f'elif gh_api_safe_quiet_print "repos/{REPO}/actions/jobs/1/logs" > "{log_file}"; then\n'
		f'\tprintf "log=%s\\n" "$(cat "{log_file}")"\n'
		"else\n"
		"\techo log_failed\n"
		"fi\n"
		'echo "rc=$?"'
	)
	result = _run(tmp_path, body, env, RELEASE_GH_API, strict=True)
	_assert_pat_fallback(result, env)
	assert "jobs=pat-output" in result.stdout
	assert "log=pat-output" in result.stdout


@pytest.mark.parametrize("job_stderr", JOB_FAILURES)
def test_dispatch_watch_reads_fall_back_to_the_pat(tmp_path: Path, job_stderr: str) -> None:
	watch = (SCRIPTS / "dispatch_and_watch_workflow_run.sh").read_text(encoding="utf-8")
	assert ". \"${SCRIPT_DIR}/comprehensive_test_and_release_gh_api.sh\"" in watch
	assert f"\t{DISPATCH_WATCH_REGISTRATION}\n" in watch
	assert DISPATCH_WATCH_STATUS in watch
	env = _env(tmp_path, FAKE_JOB_STDERR=job_stderr, FAKE_JOB_RC="1", FAKE_PAT_STDOUT="4242")
	body = (
		f'TARGET_REPO="{REPO}"\n'
		f'REGISTRATION_QUERY="repos/{REPO}/actions/workflows/x.yml/runs?event=workflow_dispatch&per_page=10"\n'
		"REGISTRATION_JQ='[.workflow_runs[]] | sort_by(.created_at) | last | .id // empty'\n"
		f"{DISPATCH_WATCH_REGISTRATION}\n"
		'printf "new_id=%s\\n" "${NEW_ID}"\n'
		f"{DISPATCH_WATCH_STATUS}\n"
		'printf "run_json=%s\\n" "${RUN_JSON}"\n'
		'echo "rc=$?"'
	)
	result = _run(tmp_path, body, env, RELEASE_GH_API, strict=True)
	_assert_pat_fallback(result, env)
	assert "new_id=4242\n" in result.stdout
	assert "run_json=4242\n" in result.stdout


@pytest.mark.parametrize("job_stderr", JOB_FAILURES)
def test_smoke_cancel_status_falls_back_to_the_pat(tmp_path: Path, job_stderr: str) -> None:
	script = SCRIPTS / "stable_orchestrate_cancel_dispatched_run.sh"
	env = _env(
		tmp_path,
		FAKE_JOB_STDERR=job_stderr,
		FAKE_JOB_RC="1",
		FAKE_PAT_STDOUT="completed",
		TEST_REPO=REPO,
		DISPATCHED_RUN_ID="4242",
		TRACKING_NUMBER="7",
		SMOKE_CANCEL_POLL_SECS="0",
	)
	wrapper = tmp_path / "cleanup_step.sh"
	wrapper.write_text(f"set -euo pipefail\nbash '{script}'\necho \"rc=$?\"\n", encoding="utf-8")
	result = subprocess.run(["bash", str(wrapper)], env=env, capture_output=True, text=True, timeout=60, check=False)
	_assert_pat_fallback(result, env)
	assert "ORCHESTRATE_SMOKE_DISPATCH_CANCEL run=4242 outcome=already_completed" in result.stderr
	assert not [call for call in _calls(env) if "-X POST" in call]


@pytest.mark.parametrize("job_stderr", JOB_FAILURES)
def test_promote_cycle_gate_runs_read_falls_back_to_the_pat(tmp_path: Path, job_stderr: str) -> None:
	promote = (SCRIPTS / "promote_main_cycle.sh").read_text(encoding="utf-8")
	assert f"\t{PROMOTE_IDLE_READ}\n" in promote
	assert f"\t{PROMOTE_WAIT_READ}\n" in promote
	function = _promote_gate_runs_read_function()
	shapes = {
		"idle": f'{PROMOTE_IDLE_READ}\nprintf "idle=%s\\n" "${{gate_runs_json}}"',
		"wait": f'for _pass in 1; do\n\t{PROMOTE_WAIT_READ}\n\tprintf "wait=%s\\n" "${{runs_json}}"\ndone',
	}
	for shape, call in shapes.items():
		run_dir = tmp_path / shape
		run_dir.mkdir()
		env = _env(
			run_dir,
			FAKE_JOB_STDERR=job_stderr,
			FAKE_JOB_RC="1",
			FAKE_JOB_STDOUT="job-output",
			GH_RETRY_MAX_ATTEMPTS="1",
			PROMOTE_CYCLE_GATE_WORKFLOW_FILE="test-and-mark-stable.yml",
		)
		body = f'{function}\n{call}\nwait\necho "rc=$?"'
		result = _run(run_dir, body, env, GH_HELPERS, strict=True)
		_assert_pat_fallback(result, env)
		assert f"{shape}=pat-output\n" in result.stdout
		assert _tokens(env) == [JOB_TOKEN, PAT]
		if shape == "idle":
			# The caller's stderr filter keeps only the contractual line.
			assert "HTTP 502" not in result.stderr
			assert "error connecting" not in result.stderr
		if "HTTP 403" in job_stderr:
			assert "GH_READ_FALLBACK reason=" in result.stderr


@pytest.mark.parametrize("overrides", ({"GH_READ_TOKEN": ""}, {"GH_READ_TOKEN_ENABLED": "false"}))
def test_promote_cycle_gate_runs_read_stays_on_the_pat_when_unset_or_disabled(
	tmp_path: Path, overrides: dict[str, str]
) -> None:
	env = _env(tmp_path, GH_RETRY_MAX_ATTEMPTS="1", PROMOTE_CYCLE_GATE_WORKFLOW_FILE="test-and-mark-stable.yml", **overrides)
	body = f'{_promote_gate_runs_read_function()}\n{PROMOTE_IDLE_READ}\nprintf "idle=%s\\n" "${{gate_runs_json}}"\nwait\necho "rc=$?"'
	result = _run(tmp_path, body, env, GH_HELPERS, strict=True)
	assert result.returncode == 0, result.stderr
	assert "idle=pat-output\n" in result.stdout
	assert _tokens(env) == [PAT]
