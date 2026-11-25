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
printf 'pat-output'
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


def _run(tmp_path: Path, body: str, env: dict[str, str], helper: Path = GH_HELPERS) -> subprocess.CompletedProcess[str]:
	script = tmp_path / "poller.sh"
	script.write_text(f"source '{helper}'\n{body}\n", encoding="utf-8")
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


@pytest.mark.parametrize("stderr", ("gh: Server Error (HTTP 502)", "error connecting to api.github.com"))
def test_other_failures_fall_back_once_to_the_pat_without_latching(tmp_path: Path, stderr: str) -> None:
	# Follow-up to #9888: a 5xx or non-HTTP job-token failure must not fail a
	# read the PAT can answer; the job-token stderr is dropped and no latch is set.
	env = _env(tmp_path, FAKE_JOB_STDERR=stderr, FAKE_JOB_RC="1")
	result = _run(tmp_path, f'gh_read api "repos/{REPO}/pulls/1"\ngh_read api "repos/{REPO}/pulls/2"', env)
	assert result.returncode == 0, result.stderr
	assert result.stdout == "pat-outputpat-output"
	assert _tokens(env) == [JOB_TOKEN, PAT, JOB_TOKEN, PAT]
	assert result.stderr.count("GH_READ_FALLBACK reason=error caller=poller.sh") == 2
	assert stderr not in result.stderr
	assert not list((tmp_path / "runner_temp").glob("gh_read_refused_*"))


def test_other_failure_with_pat_rate_limited_returns_the_job_token_error(tmp_path: Path) -> None:
	# Job token 502 while the PAT is exhausted: gh_retry must see the transient
	# job-token error (backoff + retry), not the PAT's rate limit (alert, breaker,
	# wait for the PAT reset).
	env = _env(
		tmp_path,
		FAKE_JOB_STDERR="gh: Server Error (HTTP 502)",
		FAKE_JOB_RC="1",
		FAKE_PAT_STDERR="gh: API rate limit exceeded for user ID 1. (HTTP 403)",
		FAKE_PAT_RC="1",
	)
	result = _run(tmp_path, f'gh_read api "repos/{REPO}/pulls/1"; echo "rc=$?"', env)
	assert result.stdout == "rc=1\n"
	assert _tokens(env) == [JOB_TOKEN, PAT]
	assert "GH_READ_FALLBACK reason=error caller=poller.sh" in result.stderr
	assert "HTTP 502" in result.stderr
	assert "rate limit" not in result.stderr
	assert not list((tmp_path / "runner_temp").glob("gh_read_refused_*"))


def test_pr_checks_non_zero_returns_the_job_token_result(tmp_path: Path) -> None:
	# `gh pr checks` exits 8 with a valid answer while checks are pending.
	env = _env(tmp_path, FAKE_JOB_STDOUT="pending-checks", FAKE_JOB_RC="8")
	result = _run(tmp_path, f'gh_read pr checks 1 -R {REPO}; echo " rc=$?"', env)
	assert result.stdout == "pending-checks rc=8\n"
	assert _tokens(env) == [JOB_TOKEN]
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
	# Parity with gh_read: logged as reason=error, no latch.
	assert result.stderr.count("GH_READ_FALLBACK reason=error caller=poller.sh") == 2
	assert "HTTP 500" not in result.stderr
	assert not list((tmp_path / "runner_temp").glob("gh_read_refused_*"))
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
	assert promote.count("gh_retry gh_read --same-repo-only api") == 2
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


# --- job-token failures never fail a release-gate polling step ------------------
#
# Follow-up to #9888 (unblock judge on PR #9897): every read that a step with
# GH_READ_TOKEN reaches must fall back to the PAT when the job-token call
# fails with a permission 403, a rate limit, an HTTP 502 or a non-HTTP error,
# so the step keeps the PAT's answer and exits 0. The stub below fails every
# job-token call and answers PAT calls per endpoint; `sleep` is stubbed so the
# loops run instantly. Scripts run under `set -euo pipefail` (bash -euo).

JOB_TOKEN_FAILURES = (
	pytest.param("gh: Resource not accessible by integration (HTTP 403)", "403", id="http403"),
	pytest.param("gh: API rate limit exceeded for installation ID 1. (HTTP 403)", "rate_limit", id="rate_limit"),
	pytest.param("gh: Server Error (HTTP 502)", "error", id="http502"),
	pytest.param("error connecting to api.github.com", "error", id="non_http"),
)

REVIEW_HEAD_SHA = "a" * 40

SITES_FAKE_GH = r"""#!/usr/bin/env bash
printf 'token=%s args=%s\n' "${GH_TOKEN:-}" "$*" >> "${FAKE_GH_CALLS}"
if [ "${GH_TOKEN:-}" = "${FAKE_JOB_TOKEN}" ]; then
	printf '%s\n' "${FAKE_JOB_STDERR}" >&2
	exit 1
fi
next_count()
{
	local count_file="${FAKE_STATE_DIR}/$1" count=0
	[ -f "${count_file}" ] && count="$(cat "${count_file}")"
	count=$((count + 1))
	printf '%s' "${count}" > "${count_file}"
	printf '%s' "${count}"
}
args="$*"
case "${args}" in
	"workflow run "*) exit 0 ;;
	*"-X POST "*"/cancel") exit 0 ;;
	*"/runs?event=workflow_dispatch&per_page=1 "*) echo 100 ;;
	*"/runs?event=workflow_dispatch&per_page=10 "*) echo 101 ;;
	*"actions/runs/101 --jq {status, conclusion}") echo '{"status":"completed","conclusion":"success"}' ;;
	*"actions/runs/101 --jq .status"*)
		if [ "$(next_count cancel_status)" -le 1 ]; then echo queued; else echo completed; fi
		;;
	*"actions/runs?branch=ai/issue-1&event=pull_request&per_page=5 "*)
		if [ "$(next_count review_runs)" -le 1 ]; then
			echo '{"id":555,"name":"Internal: AI Review & Autofix","path":".github/workflows/internal-review.yml","status":"in_progress","conclusion":null,"head_sha":"FAKE_SHA"}'
		else
			echo '{"id":555,"name":"Internal: AI Review & Autofix","path":".github/workflows/internal-review.yml","status":"completed","conclusion":"success","head_sha":"FAKE_SHA"}'
		fi
		;;
	*"actions/runs/555/jobs?per_page=10"*)
		echo '{"total_count":1,"jobs":[{"id":77,"name":"review / codex-agent","status":"in_progress","conclusion":null,"steps":[{"name":"Run reviewer models","status":"in_progress","conclusion":null,"started_at":"2026-11-25T00:00:00Z"}]}]}'
		;;
	*"pulls/7 --jq .head.sha"*) echo FAKE_SHA ;;
	*"pulls/7/comments"*) echo 0 ;;
	*"actions/jobs/77/logs"*) echo 'reviewer log line' ;;
	*) printf '%s' "${FAKE_PAT_STDOUT:-pat-output}" ;;
esac
""".replace("FAKE_SHA", REVIEW_HEAD_SHA)


def _sites_env(tmp_path: Path, job_stderr: str, **extra: str) -> dict[str, str]:
	env = _env(tmp_path, FAKE_JOB_STDERR=job_stderr, **extra)
	bin_dir = tmp_path / "bin"
	(bin_dir / "gh").write_text(SITES_FAKE_GH, encoding="utf-8")
	sleep_stub = bin_dir / "sleep"
	sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
	sleep_stub.chmod(0o755)
	state_dir = tmp_path / "fake_state"
	state_dir.mkdir(exist_ok=True)
	env["FAKE_STATE_DIR"] = str(state_dir)
	env["GITHUB_OUTPUT"] = str(tmp_path / "github_output")
	return env


def _run_script(script: Path, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["bash", "-euo", "pipefail", str(script), *args],
		cwd=REPO_ROOT,
		env=env,
		capture_output=True,
		text=True,
		timeout=120,
		check=False,
	)


def _assert_job_token_tried_then_pat(env: dict[str, str], result: subprocess.CompletedProcess[str], reason: str) -> None:
	tokens = _tokens(env)
	assert JOB_TOKEN in tokens, tokens
	assert tokens[-1] == PAT
	assert f"GH_READ_FALLBACK reason={reason} " in result.stderr
	assert JOB_TOKEN not in result.stdout + result.stderr
	# Every write went out with the PAT.
	for line in _calls(env):
		if "workflow run " in line or "-X POST" in line:
			assert line.startswith(f"token={PAT} "), line


@pytest.mark.parametrize("job_stderr,reason", JOB_TOKEN_FAILURES)
def test_dispatch_and_watch_survives_job_token_failures(tmp_path: Path, job_stderr: str, reason: str) -> None:
	env = _sites_env(tmp_path, job_stderr)
	result = _run_script(
		SCRIPTS / "dispatch_and_watch_workflow_run.sh",
		[
			"--repo", REPO,
			"--workflow", "x.yml",
			"--registration-timeout-secs", "60",
			"--completion-timeout-secs", "60",
		],
		env,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	outputs = Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8")
	assert "dispatched_run_id=101" in outputs
	assert "run_id=101" in outputs
	assert "workflow_conclusion=success" in outputs
	_assert_job_token_tried_then_pat(env, result, reason)


@pytest.mark.parametrize("job_stderr,reason", JOB_TOKEN_FAILURES)
def test_review_wait_survives_job_token_failures(tmp_path: Path, job_stderr: str, reason: str) -> None:
	# First poll: in_progress (jobs, PR head, PR comments and job-log reads);
	# second poll: completed/success. All reads opt in to the job token.
	env = _sites_env(
		tmp_path,
		job_stderr,
		TEST_REPO=REPO,
		ISSUE_NUMBER="1",
		PR_NUMBER="7",
		REVIEW_TIMEOUT="30",
		REVIEW_PHASE_HANDOFF_BUDGET_MINUTES="10",
		POLL_INTERVAL="1",
	)
	result = _run_script(SCRIPTS / "stable_e2e_step_wait_review.sh", [], env)
	assert result.returncode == 0, result.stdout + result.stderr
	outputs = Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8")
	assert "status=success" in outputs
	assert "review_run_id=555" in outputs
	assert "Review run #555: status=in_progress" in result.stdout
	calls = "\n".join(_calls(env))
	for endpoint in ("actions/runs/555/jobs", "pulls/7 --jq", "pulls/7/comments", "actions/jobs/77/logs"):
		assert f"token={PAT} args=api repos/{REPO}/{endpoint}" in calls, endpoint
	_assert_job_token_tried_then_pat(env, result, reason)


@pytest.mark.parametrize("job_stderr,reason", JOB_TOKEN_FAILURES)
def test_orchestrate_cancel_status_poll_survives_job_token_failures(tmp_path: Path, job_stderr: str, reason: str) -> None:
	env = _sites_env(
		tmp_path,
		job_stderr,
		TEST_REPO=REPO,
		DISPATCHED_RUN_ID="101",
		RUN_ID="",
		TRACKING_NUMBER="",
		SMOKE_CANCEL_POLL_SECS="0",
	)
	result = _run_script(SCRIPTS / "stable_orchestrate_cancel_dispatched_run.sh", [], env)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "ORCHESTRATE_SMOKE_DISPATCH_CANCEL run=101 outcome=cancelled" in result.stderr
	assert f"token={PAT} args=api -X POST repos/{REPO}/actions/runs/101/cancel" in _calls(env)
	_assert_job_token_tried_then_pat(env, result, reason)


# Each case runs the call site's own source text (asserted present in the
# script) with its variables bound, so a changed call site fails here.
GH_READ_CALL_SITES = (
	pytest.param(
		"promote_main_cycle.sh",
		'gate_runs_json="$(gh_retry gh_read --same-repo-only api "repos/${GITHUB_REPOSITORY}/actions/workflows/${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}/runs?per_page=30" 2> >(grep -a \'^GH_READ_FALLBACK \' >&2 || true) || echo "")"',
		'printf "%s" "${gate_runs_json}"',
		id="promote_idle_wait",
	),
	pytest.param(
		"promote_main_cycle.sh",
		'runs_json="$(gh_retry gh_read --same-repo-only api "repos/${GITHUB_REPOSITORY}/actions/workflows/${PROMOTE_CYCLE_GATE_WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=50")"',
		'printf "%s" "${runs_json}"',
		id="promote_gate_poll",
	),
	pytest.param(
		"workflow_failure_heal_queue.sh",
		'gh_retry gh_read --same-repo-only api --method GET --paginate --slurp "repos/${SELF_REPO}/issues/${QUEUE_ISSUE}/comments?per_page=100" > "${COMMENTS_FILE}" 2> >(grep -a \'^GH_READ_FALLBACK \' >&2 || true)',
		'cat "${COMMENTS_FILE}"',
		id="heal_queue_comments",
	),
	pytest.param(
		"workflow_failure_heal_queue.sh",
		'gh_retry gh_read --same-repo-only api --method GET "repos/${SELF_REPO}/actions/runs/${HOLDER_RUN}" > "${HOLDER_RUN_FILE}" 2> >(grep -a \'^GH_READ_FALLBACK \' >&2 || true)',
		'cat "${HOLDER_RUN_FILE}"',
		id="heal_queue_holder_run",
	),
)


@pytest.mark.parametrize("job_stderr,reason", JOB_TOKEN_FAILURES)
@pytest.mark.parametrize("script_name,call_site,show", GH_READ_CALL_SITES)
def test_gh_read_call_sites_survive_job_token_failures(
	tmp_path: Path, script_name: str, call_site: str, show: str, job_stderr: str, reason: str
) -> None:
	assert call_site in (SCRIPTS / script_name).read_text(encoding="utf-8")
	env = _sites_env(tmp_path, job_stderr)
	body = "\n".join(
		(
			"set -euo pipefail",
			f'PROMOTE_CYCLE_GATE_WORKFLOW_FILE=x.yml SELF_REPO="{REPO}" QUEUE_ISSUE=1 HOLDER_RUN=5',
			f'COMMENTS_FILE="{tmp_path}/comments.json" HOLDER_RUN_FILE="{tmp_path}/holder.json"',
			call_site,
			show,
			"wait",
		)
	)
	result = _run(tmp_path, body, env)
	assert result.returncode == 0, result.stderr
	assert result.stdout == "pat-output"
	assert _tokens(env) == [JOB_TOKEN, PAT]
	assert re.search(rf"^GH_READ_FALLBACK reason={reason} caller=poller\.sh$", result.stderr, re.MULTILINE), result.stderr
	assert job_stderr not in result.stderr
	assert JOB_TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize("job_stderr,reason", JOB_TOKEN_FAILURES)
def test_gh_review_pr_state_survives_job_token_failures(tmp_path: Path, job_stderr: str, reason: str) -> None:
	env = _sites_env(tmp_path, job_stderr, FAKE_PAT_STDOUT="closed")
	env["RUNNER_TEMP"] = ""
	result = _run(tmp_path, f"set -euo pipefail\ngh_review_pr_state {REPO} 7\nwait", env)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "closed"
	assert _tokens(env) == [JOB_TOKEN, PAT]
	assert f"GH_READ_FALLBACK reason={reason} caller=poller.sh" in result.stderr
	assert JOB_TOKEN not in result.stdout + result.stderr
