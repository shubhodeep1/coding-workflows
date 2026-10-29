#!/usr/bin/env python3
"""Check-triage target-branch guard in plan.yml (issue #7064).

The guard is default off. When CHECK_TRIAGE_BRANCH_GUARD_ENABLED is on, an
``ai:check-triage`` issue whose body declares no verified target branch is
blocked (a synthetic BLOCKED line for the existing parse/blocked-handler path)
instead of being planned on the default branch. It reads only the resolver's
status token and the issue labels, never issue prose.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
RESOLVER = REPO_ROOT / "scripts" / "resolve_integration_ref.sh"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "integration_ref_resolver"
GUARD_STEP = "Check-triage target-branch guard"
# Same detection regex as the "Parse planning output" step (perl, /i).
NEEDS_JQ = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required to read issue labels")
BLOCKED_RE = re.compile(r"^\s*BLOCKED:\s*(.*\S)\s*$", re.IGNORECASE)


def _workflow_text() -> str:
	return PLAN_WF.read_text(encoding="utf-8")


def _step_text(name: str) -> str:
	text = _workflow_text()
	marker = f"      - name: {name}\n"
	start = text.find(marker)
	assert start != -1, f"missing workflow step: {name}"
	end = text.find("\n      - name:", start + len(marker))
	# Drop trailing comment lines that belong to the next step.
	block = text[start:] if end == -1 else text[start:end]
	return block


def _step_names() -> list[str]:
	return re.findall(r"^      - name: (.+)$", _workflow_text(), re.MULTILINE)


def _run_body(name: str) -> str:
	step = _step_text(name)
	_, body = step.split("        run: |\n", 1)
	lines = []
	for line in body.split("\n"):
		if line.strip() and not line.startswith("          ") and not line.startswith("      #"):
			break
		if line.startswith("      #"):
			break
		lines.append(line)
	return textwrap.dedent("\n".join(lines))


def _run_guard(
	tmp_path: Path,
	*,
	enabled: str | None,
	labels: list[str] | None,
	status: str | None,
	body: str = "",
) -> dict:
	meta = tmp_path / "issue_meta.json"
	if labels is None:
		meta.write_text("not json", encoding="utf-8")
	else:
		meta.write_text(
			json.dumps({"number": 7064, "body": body, "labels": [{"name": n} for n in labels]}),
			encoding="utf-8",
		)
	codex_output = tmp_path / "codex_output.txt"
	github_env = tmp_path / "github_env"
	github_output = tmp_path / "github_output"
	github_env.write_text("", encoding="utf-8")
	github_output.write_text("", encoding="utf-8")
	script = tmp_path / "guard.sh"
	script.write_text(_run_body(GUARD_STEP), encoding="utf-8")
	env = {
		"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
		"ISSUE_META_FILE": str(meta),
		"CODEX_OUTPUT_FILE": str(codex_output),
		"GITHUB_ENV": str(github_env),
		"GITHUB_OUTPUT": str(github_output),
		"ISSUE_NUMBER": "7064",
	}
	if enabled is not None:
		env["CHECK_TRIAGE_BRANCH_GUARD_ENABLED"] = enabled
	if status is not None:
		env["CHECK_TRIAGE_RESOLVER_STATUS"] = status
	result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
	return {
		"rc": result.returncode,
		"stdout": result.stdout,
		"stderr": result.stderr,
		"output": codex_output.read_text(encoding="utf-8") if codex_output.exists() else None,
		"env": github_env.read_text(encoding="utf-8"),
		"gh_output": github_output.read_text(encoding="utf-8"),
	}


def _assert_blocked(result: dict, reason_fragment: str) -> None:
	assert result["rc"] == 0, result["stderr"]
	assert result["output"] is not None
	lines = [line for line in result["output"].splitlines() if line.strip()]
	assert len(lines) == 1, lines
	match = BLOCKED_RE.match(lines[0])
	assert match, lines[0]
	assert reason_fragment in match.group(1)
	assert "CHECK_TRIAGE_BRANCH_GUARD_BLOCKED=true" in result["env"].splitlines()
	assert "blocked=true" in result["gh_output"].splitlines()
	assert "outcome=blocked" in result["stdout"]


def _assert_not_blocked(result: dict, outcome: str) -> None:
	assert result["rc"] == 0, result["stderr"]
	assert result["output"] is None
	assert "CHECK_TRIAGE_BRANCH_GUARD_BLOCKED" not in result["env"]
	assert "blocked=true" not in result["gh_output"]
	assert f"outcome={outcome}" in result["stdout"]


@pytest.mark.parametrize("enabled", [None, "false", "garbage", ""])
def test_disabled_guard_never_blocks(tmp_path: Path, enabled: str | None) -> None:
	result = _run_guard(tmp_path, enabled=enabled, labels=["ai:check-triage"], status="none")
	_assert_not_blocked(result, "skip")
	assert "reason=disabled" in result["stdout"]


@NEEDS_JQ
@pytest.mark.parametrize("enabled", ["true", "TRUE", "1", "on", "yes"])
def test_enabled_guard_ignores_issues_without_check_triage_label(tmp_path: Path, enabled: str) -> None:
	for status in ("none", "failed", "unavailable", "resolved", "routed", ""):
		sub = tmp_path / (status or "empty")
		sub.mkdir()
		result = _run_guard(sub, enabled=enabled, labels=["ai:planning"], status=status)
		_assert_not_blocked(result, "pass")
		assert "reason=not_check_triage" in result["stdout"]


@NEEDS_JQ
def test_enabled_guard_passes_with_resolved_metadata(tmp_path: Path) -> None:
	result = _run_guard(tmp_path, enabled="true", labels=["ai:check-triage", "ai:planning"], status="resolved")
	_assert_not_blocked(result, "pass")
	assert "reason=metadata_present" in result["stdout"]


@NEEDS_JQ
def test_enabled_guard_passes_verified_pr_head_route_with_its_own_reason(tmp_path: Path) -> None:
	# A routed branch is a verified PR head, not body metadata: it passes,
	# but the log must not claim metadata_present.
	result = _run_guard(tmp_path, enabled="true", labels=["ai:check-triage"], status="routed")
	_assert_not_blocked(result, "pass")
	assert "reason=pr_head_routed" in result["stdout"]
	assert "metadata_present" not in result["stdout"]


@NEEDS_JQ
def test_enabled_guard_blocks_without_metadata(tmp_path: Path) -> None:
	result = _run_guard(tmp_path, enabled="true", labels=["ai:check-triage"], status="none")
	_assert_blocked(result, "no explicit target-branch metadata")
	assert "reason=no_target_branch_metadata" in result["stdout"]
	# Placeholders sit in inline code, never as a bare <tag> GitHub strips.
	assert "<branch>" not in result["output"]
	assert "`Target branch: BRANCH_NAME`" in result["output"]


@NEEDS_JQ
@pytest.mark.parametrize("status", ["failed", "unavailable", "", "resolved\nINJECTED=1", "bogus"])
def test_enabled_guard_blocks_when_branch_unverified(tmp_path: Path, status: str) -> None:
	result = _run_guard(tmp_path, enabled="true", labels=["ai:check-triage"], status=status)
	_assert_blocked(result, "could not be verified")
	assert "reason=target_branch_unverified" in result["stdout"]
	assert "INJECTED" not in result["output"]
	assert "INJECTED" not in result["env"]


@pytest.mark.parametrize("status", ["resolved", "none"])
def test_enabled_guard_fails_closed_on_unreadable_labels(tmp_path: Path, status: str) -> None:
	result = _run_guard(tmp_path, enabled="true", labels=None, status=status)
	_assert_blocked(result, "could not be verified")
	assert "issue labels were unreadable" in result["output"]
	assert "no explicit target-branch metadata" not in result["output"]
	assert "reason=labels_unavailable" in result["stdout"]


@NEEDS_JQ
def test_guard_never_infers_branch_from_issue_prose(tmp_path: Path) -> None:
	body = "The failing PR targets orchestrator/project-77.\nBase branch: orchestrator/project-77\nTarget branch: main\n"
	result = _run_guard(tmp_path, enabled="true", labels=["ai:check-triage"], status="none", body=body)
	_assert_blocked(result, "no explicit target-branch metadata")
	assert "orchestrator/project-77" not in result["output"]
	assert "project-77" not in _run_body(GUARD_STEP)


def test_guard_step_wiring_contract() -> None:
	step = _step_text(GUARD_STEP)
	body = _run_body(GUARD_STEP)
	assert "${{" not in body
	assert "id: check_triage_branch_guard" in step
	assert "if: env.SKIP_PLAN != 'true'" in step
	assert "CHECK_TRIAGE_BRANCH_GUARD_ENABLED: ${{ vars.CHECK_TRIAGE_BRANCH_GUARD_ENABLED || 'false' }}" in step
	assert "CHECK_TRIAGE_RESOLVER_STATUS: ${{ steps.refctx.outputs.resolver_status }}" in step
	names = _step_names()
	guard = names.index(GUARD_STEP)
	assert names.index("Validate planning phase label") < guard
	assert names.index("Check and claim /answer command") < guard
	assert guard < names.index("Capture pre-Codex plan write-guard snapshot")
	assert guard < names.index("Run Codex planning") < names.index("Parse planning output")
	codex = _step_text("Run Codex planning")
	assert "if: env.SKIP_PLAN != 'true' && env.CHECK_TRIAGE_BRANCH_GUARD_BLOCKED != 'true'" in codex
	for name in ("Install Claude Code CLI", "Resolve Claude credential"):
		assert "env.CHECK_TRIAGE_BRANCH_GUARD_BLOCKED != 'true'" in _step_text(name), name


def test_refctx_emits_resolver_status_on_every_exit_path() -> None:
	body = _run_body("Resolve integration ref")
	assert body.count('echo "resolver_status=unavailable" >> "$GITHUB_OUTPUT"') == 3
	# "failed" is written twice: the generic fallback and the tolerated
	# reason=pr_not_open refusal (#7477), which also writes pr_not_open=true.
	for status, expected in (("resolved", 1), ("routed", 1), ("none", 1), ("failed", 2)):
		assert body.count(f'echo "resolver_status={status}" >> "$GITHUB_OUTPUT"') == expected, status
	assert body.count('echo "pr_not_open=true" >> "$GITHUB_OUTPUT"') == 1
	# "routed" only when the resolver's own routing line names this issue and
	# exactly the branch it printed.
	assert 'bash "${resolver_script}" 2>"${resolver_err_file}"' in body
	assert '"CI_TRIAGE_PR_BRANCH_ROUTING issue=${issue_number} pr="*" outcome=routed branch=${integration_branch}") triage_routed=true ;;' in body
	# The label_missing hold (#7516, #8151) adds two more exit 0 paths, the
	# untrusted-comment skip and the completed hold. Both go through
	# hold_skip_plan, which writes resolver_status=failed in a group redirect.
	hold_exits = body.count("hold_skip_plan\n")
	assert hold_exits == 2
	assert body.count("exit 0") == body.count('echo "resolver_status=unavailable"') + hold_exits
	hold_fn = body[body.index("hold_skip_plan() {"):body.index("hold_login=")]
	for line in ('echo "label_missing_hold=true"', 'echo "resolver_status=failed"', '} >> "$GITHUB_OUTPUT"', 'echo "SKIP_PLAN=true" >> "$GITHUB_ENV"'):
		assert line in hold_fn, line
	# Checkout ref selection is unchanged.
	assert "ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}" in _workflow_text()


@pytest.mark.parametrize(
	("issue", "pr", "branch", "expected"),
	[
		("101", "55", "orchestrator/project-77", "true"),
		("102", "55", "orchestrator/project-77", "false"),
		("101", "55", "orchestrator/project-78", "false"),
	],
)
def test_refctx_routed_detection_matches_resolver_line(tmp_path: Path, issue: str, pr: str, branch: str, expected: str) -> None:
	# Run the workflow's own detection loop on the line the resolver emits.
	body = _run_body("Resolve integration ref")
	start = body.index("triage_routed=false")
	end = body.index('done < "${resolver_err_file}"') + len('done < "${resolver_err_file}"')
	loop = textwrap.dedent(body[start:end])
	match = re.search(r'echo "(CI_TRIAGE_PR_BRANCH_ROUTING [^"]*outcome=routed[^"]*)" >&2', RESOLVER.read_text(encoding="utf-8"))
	assert match, "resolver routing line not found"
	line = match.group(1).replace("${ISSUE}", issue).replace("${pr}", pr).replace("${ref}", branch)
	err = tmp_path / "resolver.err"
	err.write_text(line + "\n", encoding="utf-8")
	script = f'issue_number=101\nintegration_branch=orchestrator/project-77\nresolver_err_file="{err}"\n{loop}\necho "$triage_routed"\n'
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == expected


def test_resolver_fixtures_for_check_triage_bodies() -> None:
	prose = json.loads((FIXTURE_DIR / "check_triage_prose_branch_not_inferred.json").read_text(encoding="utf-8"))
	target = json.loads((FIXTURE_DIR / "check_triage_target_branch_main.json").read_text(encoding="utf-8"))
	assert prose["expected_stdout"] == "" and prose["expected_exit_code"] == 0
	assert "orchestrator/project-77" in prose["issues"]["101"]["body"]
	assert target["expected_stdout"] == "main" and target["expected_exit_code"] == 0
	result = subprocess.run(["bash", str(RESOLVER), "--self-test"], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "self-test passed" in result.stdout


# ---------------------------------------------------------------------------
# Failure path after an early resolver refusal (heal #7526, source #7511).
#
# "Resolve integration ref" runs before "Checkout repository", so a resolver
# refusal fails the job with no scripts/ tree and no RUNTIME_DIR. The
# failure/always steps below used to add misleading errors in that state
# (run 38989786896); they now skip with a ::warning:: instead.
# ---------------------------------------------------------------------------

FAILURE_PATH_GUARDED_STEPS = {
	"Complete /answer command (failure/cancel)": "memory_helpers.sh",
	"Record planning run failed": "memory_helpers.sh",
	"Revert label on planning failure": "label_helpers.sh",
	"Telegram failure notification": "tg_helpers.sh",
}

FAILURE_PATH_STEP_GATES = {
	"Complete /answer command (failure/cancel)": "if: (failure() || cancelled()) && env.SKIP_PLAN != 'true' && env.PROCESSED_ANSWER_CLAIMED == 'true' && env.PROCESSED_ANSWER_COMPLETED != 'true'",
	"Record planning run failed": "if: (failure() || cancelled()) && env.SKIP_PLAN != 'true'",
	"Revert label on planning failure": "if: failure() || cancelled()",
	"Telegram failure notification": "if: failure() || cancelled()",
	"Write run summary": "if: env.SKIP_PLAN != 'true' && always()",
}

FAILURE_PATH_STUB_FUNCTIONS = {
	"Complete /answer command (failure/cancel)": "memory_processed_command_complete",
	"Record planning run failed": "memory_record_run_event",
	"Revert label on planning failure": "ensure_label_exists",
	"Telegram failure notification": "tg_send_phase_failure",
}


def _failure_path_body(name: str) -> str:
	body = _run_body(name)
	for expr, value in (
		("${{ job.status }}", "failure"),
		("${{ github.repository }}", "example/repo"),
		("${{ github.run_id }}", "123456"),
		("${{ github.actor }}", "example-actor"),
		("${{ github.server_url }}", "https://github.com"),
	):
		body = body.replace(expr, value)
	assert "${{" not in body, name
	return body


def _run_failure_path_step(body: str, cwd: Path, extra_env: dict | None = None) -> dict:
	fake_bin = cwd / "_fake_bin"
	fake_bin.mkdir(exist_ok=True)
	gh_log = cwd / "_gh_calls.log"
	gh = fake_bin / "gh"
	gh.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{gh_log}"\nexit 1\n', encoding="utf-8")
	gh.chmod(0o755)
	step_file = cwd / "_step.sh"
	step_file.write_text(body, encoding="utf-8")
	github_env = cwd / "_github_env"
	summary = cwd / "_step_summary"
	github_env.write_text("", encoding="utf-8")
	summary.write_text("", encoding="utf-8")
	env = {
		"PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
		"HOME": str(cwd),
		"ISSUE_NUMBER": "7511",
		"ISSUE_URL": "https://github.com/example/repo/issues/7511",
		"MODEL_EDITOR": "example-model",
		"PLAN_FAILURE_CONTEXT": "AI Plan phase failed before posting the implementation plan.",
		"TOKEN_WARN_THRESHOLD": "200000",
		"TRIGGER_COMMENT_ID": "42",
		"GITHUB_ENV": str(github_env),
		"GITHUB_STEP_SUMMARY": str(summary),
	}
	if extra_env:
		env.update(extra_env)
	result = subprocess.run(
		["bash", "--noprofile", "--norc", "-e", str(step_file)],
		cwd=str(cwd),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)
	return {
		"rc": result.returncode,
		"stdout": result.stdout,
		"stderr": result.stderr,
		"gh_calls": gh_log.read_text(encoding="utf-8") if gh_log.exists() else "",
		"summary": summary.read_text(encoding="utf-8"),
	}


@pytest.mark.parametrize("name", sorted(FAILURE_PATH_GUARDED_STEPS))
def test_failure_path_steps_skip_without_checkout(tmp_path: Path, name: str) -> None:
	helper = FAILURE_PATH_GUARDED_STEPS[name]
	assert not (tmp_path / "scripts").exists()
	result = _run_failure_path_step(_failure_path_body(name), tmp_path)
	assert result["rc"] == 0, result["stderr"]
	assert "No such file or directory" not in result["stderr"]
	warnings = [line for line in result["stdout"].splitlines() if line.startswith("::warning::")]
	assert len(warnings) == 1, result["stdout"]
	assert f"scripts/{helper}" in warnings[0]
	assert warnings[0].startswith(f"::warning::{name}: skipped")
	assert result["gh_calls"] == ""


def test_write_run_summary_without_runtime_dir(tmp_path: Path) -> None:
	result = _run_failure_path_step(_failure_path_body("Write run summary"), tmp_path)
	assert result["rc"] == 0, result["stderr"]
	assert "unbound variable" not in result["stderr"]
	assert any(
		line.startswith("::warning::") and "RUNTIME_DIR" in line for line in result["stdout"].splitlines()
	), result["stdout"]
	assert "## AI Plan Summary" in result["summary"]
	assert "| Issue | #7511 |" in result["summary"]
	assert "Tokens used" not in result["summary"]


def test_write_run_summary_with_runtime_dir_keeps_token_row(tmp_path: Path) -> None:
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	(runtime / "codex_log.txt").write_text("tokens used\n1,234\n", encoding="utf-8")
	result = _run_failure_path_step(
		_failure_path_body("Write run summary"), tmp_path, {"RUNTIME_DIR": str(runtime)}
	)
	assert result["rc"] == 0, result["stderr"]
	assert "::warning::" not in result["stdout"]
	assert "| Tokens used | 1,234 |" in result["summary"]


@NEEDS_JQ
@pytest.mark.parametrize("name", sorted(FAILURE_PATH_GUARDED_STEPS))
def test_failure_path_steps_still_source_staged_helpers(tmp_path: Path, name: str) -> None:
	helper = FAILURE_PATH_GUARDED_STEPS[name]
	func = FAILURE_PATH_STUB_FUNCTIONS[name]
	stub_log = tmp_path / "_stub.log"
	scripts = tmp_path / "scripts"
	scripts.mkdir()
	(scripts / helper).write_text(f'{func}() {{ echo "{func}" >> "$STUB_LOG"; }}\n', encoding="utf-8")
	extra = {"STUB_LOG": str(stub_log)}
	if name == "Revert label on planning failure":
		# Labels include ai:planning so the rollback reaches ensure_label_exists.
		meta = tmp_path / "issue_meta.json"
		meta.write_text(json.dumps({"labels": [{"name": "ai:planning"}]}), encoding="utf-8")
		extra["ISSUE_META_FILE"] = str(meta)
		# The real support tree provides gh_retry through gh_helpers.sh.
		(scripts / "gh_helpers.sh").write_text('gh_retry() { "$@"; }\n', encoding="utf-8")
	result = _run_failure_path_step(_failure_path_body(name), tmp_path, extra)
	assert result["rc"] == 0, result["stderr"]
	assert "skipped" not in result["stdout"]
	assert stub_log.exists() and func in stub_log.read_text(encoding="utf-8")


def test_failure_path_step_gates_unchanged() -> None:
	for name, gate in FAILURE_PATH_STEP_GATES.items():
		step = _step_text(name)
		assert f"        {gate}\n" in step, name
	assert len(_workflow_text().encode("utf-8")) < 480_000
