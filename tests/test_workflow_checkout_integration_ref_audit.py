#!/usr/bin/env python3
"""Audit actions/checkout@v5 workflows for integration-ref safety contracts."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

REQUIRED_RESOLVER_WORKFLOWS = {
	"plan.yml",
	"clarify.yml",
	"orchestrate_clarify_respond.yml",
	"implement.yml",
	"validate.yml",
}

# Explicit allow-list exceptions for checkout@v5 workflows that are not
# orchestrator-managed issue execution paths.
ALLOWLIST_EXCEPTIONS = {
	"audit_consumer_drift.yml": "Consumer-wrapper drift audit is scheduled/manual repository maintenance, not an orchestrator issue-phase checkout path.",
	"cancel_on_pr_close.yml": "PR-close cleanup cancels branch runs and has no orchestrator issue-phase checkout.",
	"ci.yml": "PR CI validation has no orchestrator issue metadata.",
	"integration-pr-readiness.yml": "Integration-PR readiness check runs on pull_request refs and posts commit status metadata, not orchestrator issue-phase checkout.",
	"issue_pr_status.yml": "Issue/PR status utility workflow does not execute orchestrator issue phases.",
	"unblock_judge.yml": "Unblock judge checks out the default branch read-only for the model; it acts through the API on one blocked item, not on an issue-phase integration ref.",
	"lint-plan-archival.yml": "Plan-archival lint validates pull_request body/diff state rather than orchestrator issue-phase integration refs.",
	"lint-pr-body-auto-close.yml": "PR-body auto-close lint validates pull_request metadata rather than orchestrator issue-phase checkout.",
	"mark-stable.yml": "Release promotion workflow operates on repo refs, not tracking-issue metadata.",
	"memory_maintenance.yml": "Maintenance workflow has no issue-comment or tracking-issue context.",
	"orchestrate.yml": "Project bootstrap workflow has no integration-branch metadata at checkout time.",
	"orchestrate_poll.yml": "Poller handles multiple tracking issues per run; a single checkout integration ref is undefined.",
	"review_autofix.yml": "PR review/autofix operates on PR refs rather than orchestrator integration metadata.",
	"review_autofix_sweep.yml": "Scheduled review and CI recovery checks out the trusted default branch, not an issue integration ref.",
	"security-audit.yml": "Scheduled/manual default-branch security audit is a source-repo maintenance workflow, not an orchestrator issue-phase checkout path.",
	"sync_ai_labels.yml": "Repository label-sync maintenance manages ai:* labels and does not execute orchestrator issue phases.",
	"test-and-mark-stable.yml": "Release test workflow checks specific refs/tags and is outside orchestrator phase execution.",
	"comprehensive-test-and-release.yml": "Comprehensive release conductor dispatches downstream workflows and does not execute tracking-issue integration-ref checkout.",
	"drift-audit.yml": "Scheduled maintenance workflow audits review-autofix logs and is not an orchestrator issue-phase checkout path.",
	"check_failure_triage.yml": "Check-run triage operates on failing PR-check metadata and PR head refs, not orchestrator issue-phase integration refs.",
	"check-failure-triage-historical.yml": "Single-use historical triage checks out one closed PR's failing head (or merge commit) by SHA in a credential-free reproduction job, not orchestrator issue-phase integration refs.",
	"update_workflows.yml": "Workflow-template sync job is repository maintenance, not issue-phase execution.",
	"validation-improvements-intake.yml": "Validation prompt intake workflow is repository_dispatch PR automation.",
	"validation-refresh.yml": "Validation refresh workflow iterates consumer repos and is not an orchestrator issue-phase checkout path.",
	"workflow-log-analysis.yml": "Workflow-log analyzer inspects run artifacts, not orchestrator issue branches.",
	"nightly-validation-selftest.yml": "Nightly fixture self-test runs on schedule/workflow_dispatch without orchestrator issue metadata.",
	"workspace-cache-maintenance.yml": "Scheduled workspace-cache pruning operates on repository cache metadata, not orchestrator issue-phase integration refs.",
	"forward-merge-stable-to-main.yml": "Stable→main forward-merge workflow operates on repo refs (stable, main), not tracking-issue metadata.",
	"sync-claude-live-copies.yml": "Push-to-main maintenance copies template-only .claude changes to the live copy; it reads no tracking-issue metadata.",
	"promote-candidate-follow.yml": "Push-to-main/hourly maintenance fast-forwards the promote-candidate branch through the API from a main checkout; it reads no tracking-issue metadata.",
	"promote-main-to-stable.yml": "Main→stable promotion workflow operates on repo refs (main, stable), not tracking-issue metadata.",
	"auto-release-stable.yml": "Scheduled stable-branch release check operates on repo refs (stable branch vs stable tag), not tracking-issue metadata.",
	"workflow_failure_heal.yml": "Escalation reporter checks out the repo only to read consumer wrapper release pins and dispatches upstream; it executes no orchestrator issue phase.",
	"workflow-failure-heal-intake.yml": "Heal intake is repository_dispatch / workflow_run issue-filing automation on the default branch, not an orchestrator issue-phase checkout path.",
	"internal-cancel-on-pr-close.yml": "Heal PR reconcile checks out main on pull_request close to merge or close heal PRs of the closed PR; it executes no orchestrator issue phase.",
	"claude-engine-smoke.yml": "Dispatch-only Claude engine self-test checks out the dispatched ref to exercise its own scripts; it executes no orchestrator issue phase.",
	"publish-sandbox-images.yml": "Push-to-main/stable and weekly maintenance builds and pushes the sandbox images from a fixed branch ref; it reads no tracking-issue metadata.",
	"propagate-consumer-secrets.yml": "Push-to-main / dispatch maintenance copies the library's Actions secrets into registered consumers; it reads the consumer registry on main and no tracking-issue metadata.",
	"pr6576-shard-followup.yml": "One-time default-branch follow-up for PR #6576 reads CI job logs and posts one result through the API; it executes no orchestrator issue phase.",
}


def _workflow_text(filename: str) -> str:
	return (WORKFLOWS_DIR / filename).read_text(encoding="utf-8")


def _checkout_workflow_files() -> set[str]:
	files: set[str] = set()
	for path in WORKFLOWS_DIR.glob("*.yml"):
		if "uses: actions/checkout@v5" in path.read_text(encoding="utf-8"):
			files.add(path.name)
	return files


def test_checkout_workflows_are_all_classified() -> None:
	checkout_files = _checkout_workflow_files()
	classified = REQUIRED_RESOLVER_WORKFLOWS | set(ALLOWLIST_EXCEPTIONS.keys())
	missing = sorted(checkout_files - classified)
	extra = sorted(classified - checkout_files)
	assert not missing, f"Unclassified checkout@v5 workflows: {missing}"
	assert not extra, f"Classified workflows without checkout@v5: {extra}"


def test_allowlist_entries_have_rationale() -> None:
	for workflow_name, rationale in ALLOWLIST_EXCEPTIONS.items():
		assert rationale.strip(), f"Missing allow-list rationale for {workflow_name}"


def test_required_workflows_enforce_integration_ref_contract() -> None:
	resolver_step = "- name: Resolve integration ref"
	resolver_id = "id: refctx"
	canonical_stage = "resolver_stage_root=\"${RUNNER_TEMP}/resolve-integration-ref-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}\""
	canonical_exec = "bash \"${resolver_script}\""
	base_disallowed_inline_markers = (
		"contents/scripts/resolve_integration_ref.sh?ref=${resolver_ref}",
		"base64 --decode >",
		"sed -nE 's/^- Integration branch:",
		"grep -Eq '^orchestrator/project-[0-9]+$'",
		"/git/ref/heads/",
	)

	# Keep the GitHub ref-lookup ban shared so future baseline-resolution logic
	# cannot slip into another required workflow without updating this audit.
	for workflow_name in sorted(REQUIRED_RESOLVER_WORKFLOWS):
		wf = _workflow_text(workflow_name)
		disallowed_inline_markers = base_disallowed_inline_markers
		if workflow_name == "implement.yml":
			checkout_ref = "ref: ${{ steps.checkout_ref.outputs.ref || steps.refctx.outputs.ref || github.event.repository.default_branch }}"
			resolved_ref_env = "IMPLEMENT_RESOLVED_FALLBACK_REF: ${{ steps.checkout_ref.outputs.ref || steps.refctx.outputs.ref || github.event.repository.default_branch }}"
			resolved_ref_log = 'echo "Resolved fallback ref: ${IMPLEMENT_RESOLVED_FALLBACK_REF}"'
			unsafe_resolved_ref_log = "echo \"Resolved fallback ref: ${{ steps.checkout_ref.outputs.ref || steps.refctx.outputs.ref || github.event.repository.default_branch }}\""
			resolved_base_env = "IMPLEMENT_LOG_PR_BASE_REF: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}"
			resolved_base_log = 'echo "PR base ref: ${IMPLEMENT_LOG_PR_BASE_REF}"'
			unsafe_resolved_base_log = "echo \"PR base ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}\""
			checkout_resolver_step = "- name: Resolve checkout ref"
			checkout_resolver_id = "id: checkout_ref"
		else:
			# validate.yml authorizes an explicit `target_ref` against a
			# project PR and pins its head SHA, which wins over the tracking
			# issue's integration branch.
			ref_expression = (
				"steps.authorized_target.outputs.sha || steps.refctx.outputs.ref || github.event.repository.default_branch"
				if workflow_name == "validate.yml"
				else "steps.refctx.outputs.ref || github.event.repository.default_branch"
			)
			checkout_ref = f"ref: ${{{{ {ref_expression} }}}}"
			resolved_ref_name = {
				"clarify.yml": "CLARIFY_RESOLVED_REF",
				"orchestrate_clarify_respond.yml": "ORCHESTRATE_CLARIFY_RESOLVED_REF",
				"plan.yml": "PLAN_RESOLVED_REF",
				"validate.yml": "VALIDATE_RESOLVED_REF",
			}[workflow_name]
			resolved_ref_env = f"{resolved_ref_name}: ${{{{ {ref_expression} }}}}"
			resolved_ref_log = f'echo "Resolved ref: ${{{resolved_ref_name}}}"'
			unsafe_resolved_ref_log = "echo \"Resolved ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}\""
			resolved_base_env = ""
			resolved_base_log = ""
			unsafe_resolved_base_log = ""
			checkout_resolver_step = ""
			checkout_resolver_id = ""

		assert resolver_step in wf, f"{workflow_name} missing integration ref resolver step"
		assert resolver_id in wf, f"{workflow_name} missing resolver step id refctx"
		assert checkout_ref in wf, f"{workflow_name} checkout is missing refctx/default branch ref"
		assert resolved_ref_env in wf, f"{workflow_name} missing env-bound resolved-ref log input"
		assert resolved_ref_log in wf, f"{workflow_name} missing resolved-ref log output"
		assert unsafe_resolved_ref_log not in wf, f"{workflow_name} interpolates resolver output into shell source"
		if resolved_base_log:
			assert resolved_base_env in wf, f"{workflow_name} missing env-bound base-ref log input"
			assert resolved_base_log in wf, f"{workflow_name} missing base-ref log output"
			assert unsafe_resolved_base_log not in wf, f"{workflow_name} interpolates base-ref output into shell source"
			assert checkout_resolver_step in wf, f"{workflow_name} missing checkout override resolver step"
			assert checkout_resolver_id in wf, f"{workflow_name} missing checkout override resolver id"
		assert "git rev-parse HEAD" in wf, f"{workflow_name} missing HEAD commit log"
		assert "git symbolic-ref --short HEAD" in wf, f"{workflow_name} missing branch/detached log"
		assert canonical_stage in wf, f"{workflow_name} missing canonical resolver staging"
		assert canonical_exec in wf, f"{workflow_name} missing canonical resolver invocation"

		for marker in disallowed_inline_markers:
			assert marker not in wf, f"{workflow_name} still contains inline resolver marker: {marker}"

		resolver_idx = wf.find(resolver_step)
		checkout_resolver_idx = wf.find(checkout_resolver_step) if checkout_resolver_step else -1
		checkout_ref_idx = wf.find(checkout_ref)
		assert resolver_idx != -1 and checkout_ref_idx != -1 and resolver_idx < checkout_ref_idx, (
			f"{workflow_name} must resolve integration ref before refctx-bound checkout"
		)
		if checkout_resolver_step:
			assert checkout_resolver_idx != -1 and resolver_idx < checkout_resolver_idx < checkout_ref_idx, (
				f"{workflow_name} must resolve the baseline checkout override after refctx and before checkout"
			)


def _refctx_step(wf: str) -> str:
	start = wf.index("- name: Resolve integration ref")
	end = wf.find("\n      - name:", start + 1)
	return wf[start:] if end == -1 else wf[start:end]


def test_required_workflows_fail_closed_on_ci_triage_routing_refusal() -> None:
	"""Resolver exit 3 (CI-triage routing refused) must never fall back to main."""
	env_line = "CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED: ${{ vars.CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED || 'true' }}"
	forward = 'CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED="${CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED:-true}" \\'
	capture = '})" || resolver_rc=$?'
	refused_branch = 'elif [ "${resolver_rc}" -eq 3 ]; then'
	fallback = 'echo "::warning::Canonical integration resolver failed; falling back to default branch."'
	for workflow_name in sorted(REQUIRED_RESOLVER_WORKFLOWS):
		step = _refctx_step(_workflow_text(workflow_name))
		assert env_line in step, f"{workflow_name} refctx missing routing flag env"
		assert forward in step, f"{workflow_name} refctx does not forward the routing flag"
		assert "resolver_rc=0" in step and capture in step, f"{workflow_name} refctx missing resolver rc capture"
		assert 'if [ "${resolver_rc}" -eq 0 ]; then' in step, f"{workflow_name} refctx missing rc 0 branch"
		assert refused_branch in step, f"{workflow_name} refctx missing exit-3 branch"
		refused = step[step.index(refused_branch):step.index(fallback)]
		assert "exit 1" in refused, f"{workflow_name} exit-3 branch must fail the step"
		assert "GITHUB_OUTPUT" not in refused, f"{workflow_name} exit-3 branch must not write a ref"
		assert fallback in step, f"{workflow_name} non-3 failure lost its default-branch fallback"
		assert "${{ steps.refctx" not in step, f"{workflow_name} refctx interpolates its own output"


# Issue #7283: a fake `git` for the resolver staging block. FAKE_GIT_MODE:
# ok (every call succeeds), clone_fail (every clone fails), fetch_fail (the
# pinned fetch fails), fallback_only (the first clone fails, the second
# succeeds) and no_script (clones succeed without the resolver script).
_FAKE_GIT = """#!/usr/bin/env bash
args=("$@")
sub=""
i=0
while [ "$i" -lt "${#args[@]}" ]; do
  case "${args[$i]}" in
    -c|-C) i=$((i + 2)); continue ;;
    clone|fetch|checkout) sub="${args[$i]}"; break ;;
  esac
  i=$((i + 1))
done
case "$sub" in
  clone)
    n="$(cat "$FAKE_GIT_STATE" 2>/dev/null || echo 0)"
    echo "$((n + 1))" > "$FAKE_GIT_STATE"
    case "$FAKE_GIT_MODE" in
      clone_fail) exit 128 ;;
      fallback_only) [ "$n" -eq 0 ] && exit 128 ;;
    esac
    dest="${args[${#args[@]}-1]}"
    mkdir -p "$dest/scripts"
    if [ "$FAKE_GIT_MODE" != no_script ]; then
      cat > "$dest/scripts/resolve_integration_ref.sh" <<'STUB'
#!/usr/bin/env bash
echo STUB_RESOLVER_RAN >&2
if [ -n "${FAKE_RESOLVER_STDERR:-}" ]; then echo "${FAKE_RESOLVER_STDERR}" >&2; fi
exit "${FAKE_RESOLVER_RC:-0}"
STUB
    fi
    exit 0 ;;
  fetch) [ "$FAKE_GIT_MODE" = fetch_fail ] && exit 1; exit 0 ;;
esac
exit 0
"""


# Issue #7516: a fake `gh` for the clarify label_missing hold. Every call's
# argv is written NUL-separated to $FAKE_GH_DIR/call_<n>. FAKE_GH_LOGIN is the
# `api user` login, FAKE_GH_COMMENTS_JSON the comment read and
# FAKE_GH_FAIL=user|comments|comment_post|label_post fails that call.
_FAKE_GH = """#!/usr/bin/env bash
n="$(ls "$FAKE_GH_DIR" | wc -l | tr -d ' ')"
printf '%s\\0' "$@" > "$FAKE_GH_DIR/call_$((n + 1))"
method=GET
endpoint=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-X" ]; then method="$a"; fi
  case "$a" in user|repos/*) [ -z "$endpoint" ] && endpoint="$a" ;; esac
  prev="$a"
done
case "$method $endpoint" in
  "GET user") [ "${FAKE_GH_FAIL:-}" = user ] && exit 1; echo "${FAKE_GH_LOGIN:-pipeline-bot}" ;;
  "GET repos/"*"/comments"*) [ "${FAKE_GH_FAIL:-}" = comments ] && exit 1; echo "${FAKE_GH_COMMENTS_JSON:-[[]]}" ;;
  "POST repos/"*"/comments") [ "${FAKE_GH_FAIL:-}" = comment_post ] && exit 1; echo '{}' ;;
  "POST repos/"*"/labels") [ "${FAKE_GH_FAIL:-}" = label_post ] && exit 1; echo '[]' ;;
  *) exit 1 ;;
esac
exit 0
"""


def _step_text(wf: str, name: str) -> str:
	start = wf.index(f"      - name: {name}\n")
	end = wf.find("\n      - name:", start + 1)
	heal = wf.find("\n  heal-report:", start + 1)
	if heal != -1 and (end == -1 or heal < end):
		end = heal
	return wf[start:] if end == -1 else wf[start:end]


def _step_run_body(workflow_name: str, step_name: str) -> str:
	step = _step_text(_workflow_text(workflow_name), step_name)
	lines = step.splitlines()
	start = next(i for i, line in enumerate(lines) if line.strip() == "run: |")
	body = textwrap.dedent("\n".join(lines[start + 1:])) + "\n"

	def _sub(match: re.Match[str]) -> str:
		return "consumer/repo" if "github.repository" in match.group(0) else "a" * 40

	return re.sub(r"\$\{\{[^}]*\}\}", _sub, body)


def _refctx_run_body(workflow_name: str) -> str:
	step = _refctx_step(_workflow_text(workflow_name))
	lines = step.splitlines()
	start = next(i for i, line in enumerate(lines) if line.strip() == "run: |")
	body = textwrap.dedent("\n".join(lines[start + 1:])) + "\n"

	def _sub(match: re.Match[str]) -> str:
		return "consumer/repo" if "github.repository" in match.group(0) else "a" * 40

	return re.sub(r"\$\{\{[^}]*\}\}", _sub, body)


def _run_refctx(workflow_name: str, mode: str, routing: str | None, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
	rc, out, gh_output, _env_text, _calls = _run_refctx_core(workflow_name, mode, routing, extra_env)
	return rc, out, gh_output


def _run_refctx_full(workflow_name: str, mode: str, routing: str | None, extra_env: dict[str, str] | None = None) -> tuple[int, str, str, str]:
	"""Like _run_refctx, plus the text the step wrote to GITHUB_ENV."""
	rc, out, gh_output, env_text, _calls = _run_refctx_core(workflow_name, mode, routing, extra_env)
	return rc, out, gh_output, env_text


def _run_refctx_gh(workflow_name: str, mode: str, routing: str | None, extra_env: dict[str, str] | None = None) -> tuple[int, str, str, list[list[str]]]:
	rc, out, gh_output, _env_text, calls = _run_refctx_core(workflow_name, mode, routing, extra_env)
	return rc, out, gh_output, calls


def _run_refctx_core(workflow_name: str, mode: str, routing: str | None, extra_env: dict[str, str] | None = None) -> tuple[int, str, str, str, list[list[str]]]:
	"""Run the refctx step; return rc, output, GITHUB_OUTPUT, GITHUB_ENV and gh calls."""
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		fake_git = bin_dir / "git"
		fake_git.write_text(_FAKE_GIT, encoding="utf-8")
		fake_git.chmod(0o755)
		fake_gh = bin_dir / "gh"
		fake_gh.write_text(_FAKE_GH, encoding="utf-8")
		fake_gh.chmod(0o755)
		gh_dir = tmp_path / "gh_calls"
		gh_dir.mkdir()
		output = tmp_path / "github_output"
		output.write_text("", encoding="utf-8")
		github_env = tmp_path / "github_env"
		github_env.write_text("", encoding="utf-8")
		env = {
			"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
			"ISSUE_NUMBER": "5",
			"TRACKING_ISSUE": "5",
			"RUNNER_TEMP": str(tmp_path),
			"GITHUB_OUTPUT": str(output),
			"GITHUB_ENV": str(github_env),
			"GITHUB_RUN_ID": "1",
			"GITHUB_RUN_ATTEMPT": "1",
			"GH_TOKEN": "x",
			"RETARGET_DEFAULT_BRANCH": "main",
			"FAKE_GIT_MODE": mode,
			"FAKE_GIT_STATE": str(tmp_path / "clone_count"),
			"FAKE_GH_DIR": str(gh_dir),
		}
		if routing is not None:
			env["CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED"] = routing
		if extra_env:
			env.update(extra_env)
		res = subprocess.run(
			["bash", "-c", _refctx_run_body(workflow_name)],
			env=env, capture_output=True, text=True, timeout=60,
		)
		calls = []
		for path in sorted(gh_dir.iterdir(), key=lambda p: int(p.name.split("_")[1])):
			raw = path.read_text(encoding="utf-8")
			calls.append(raw.split("\0")[:-1])
		return (res.returncode, res.stdout + res.stderr, output.read_text(encoding="utf-8"),
			github_env.read_text(encoding="utf-8"), calls)


def test_required_workflows_fail_closed_when_resolver_staging_fails() -> None:
	"""Issue #7283: with CI-triage routing on, a resolver that cannot be
	staged, pinned or found fails the step without writing a ref; with it
	off, the default-branch fallback is unchanged."""
	failures = {
		"clone_fail": "stage_failed",
		"no_script": "script_missing",
		"fetch_fail": "pin_failed",
		"fallback_only": "pin_failed",
	}
	for workflow_name in sorted(REQUIRED_RESOLVER_WORKFLOWS):
		for mode, reason in failures.items():
			# Unset routes by default, like scripts/resolve_integration_ref.sh.
			for routing in ("true", "TRUE", "1", "yes", "on", None):
				rc, out, gh_output = _run_refctx(workflow_name, mode, routing)
				label = f"{workflow_name} mode={mode} routing={routing}"
				assert rc != 0, f"{label}: expected failure\n{out}"
				assert "ref=" not in gh_output, f"{label}: wrote a ref: {gh_output!r}"
				assert f"INTEGRATION_REF_RESOLVE outcome=resolver_unavailable reason={reason}" in out, f"{label}\n{out}"
				assert "STUB_RESOLVER_RAN" not in out, f"{label}: ran an unpinned resolver"
			for routing in ("false", "OFF", "0", "maybe"):
				rc, out, gh_output = _run_refctx(workflow_name, mode, routing)
				label = f"{workflow_name} mode={mode} routing={routing}"
				assert rc == 0, f"{label}: expected the fallback\n{out}"
				assert "ref=\n" in gh_output, f"{label}: no fallback ref: {gh_output!r}"
				assert "resolver_unavailable" not in out, f"{label}\n{out}"
				ran = "STUB_RESOLVER_RAN" in out
				assert ran == (mode in ("fetch_fail", "fallback_only")), f"{label}: resolver ran={ran}\n{out}"
		rc, out, gh_output = _run_refctx(workflow_name, "ok", "true")
		assert rc == 0 and "STUB_RESOLVER_RAN" in out, f"{workflow_name} ok path\n{out}"


def test_clarify_tolerates_pr_not_open_and_routing_fails_closed_on_rate_limit() -> None:
	"""A merged/closed triage PR (reason=pr_not_open) keeps the default
	branch in clarify, so the #7095 resolution gate can run, and in plan,
	whose superseded-PR step (#7216, #7477) closes the issue, plans on the
	PR's base or skips planning; every other phase still refuses. With
	routing on, a rate-limited resolver (exit 75) never falls back to the
	default branch."""
	pr_not_open = {
		"FAKE_RESOLVER_RC": "3",
		"FAKE_RESOLVER_STDERR": "::error::CI_TRIAGE_PR_BRANCH_ROUTING issue=5 pr=7 outcome=refused reason=pr_not_open",
	}
	other_refusal = {
		"FAKE_RESOLVER_RC": "3",
		"FAKE_RESOLVER_STDERR": "::error::CI_TRIAGE_PR_BRANCH_ROUTING issue=5 pr=7 outcome=refused reason=branch_missing",
	}
	rate_limited = {"FAKE_RESOLVER_RC": "75"}
	for workflow_name in sorted(REQUIRED_RESOLVER_WORKFLOWS):
		rc, out, gh_output = _run_refctx(workflow_name, "ok", "true", pr_not_open)
		if workflow_name in ("clarify.yml", "plan.yml"):
			assert rc == 0 and "ref=\n" in gh_output, f"{workflow_name} pr_not_open\n{out}"
			assert "::notice::CI_TRIAGE_PR_BRANCH_ROUTING" in out, out
			assert "::error::CI_TRIAGE_PR_BRANCH_ROUTING" not in out, out
			if workflow_name == "plan.yml":
				assert "pr_not_open=true\n" in gh_output, gh_output
				assert "resolver_status=failed\n" in gh_output, gh_output
		elif workflow_name == "implement.yml":
			# Issue #7540: implement skips without a ref and hands the issue off.
			assert rc == 0, f"{workflow_name} pr_not_open\n{out}"
			assert "ref=" not in gh_output, f"{workflow_name} wrote a ref: {gh_output!r}"
			assert "pr_not_open=true\n" in gh_output and "pr_not_open_pr=7\n" in gh_output, gh_output
			assert "::notice::CI_TRIAGE_PR_BRANCH_ROUTING" in out, out
			assert "::error::CI_TRIAGE_PR_BRANCH_ROUTING" not in out, out
		else:
			assert rc != 0 and "ref=" not in gh_output, f"{workflow_name} pr_not_open\n{out}"
		rc, out, gh_output = _run_refctx(workflow_name, "ok", "true", other_refusal)
		assert rc != 0 and "ref=" not in gh_output, f"{workflow_name} other refusal\n{out}"
		for routing in ("true", None):
			rc, out, gh_output = _run_refctx(workflow_name, "ok", routing, rate_limited)
			assert rc != 0 and "ref=" not in gh_output, f"{workflow_name} rate_limited routing={routing}\n{out}"
			assert "INTEGRATION_REF_RESOLVE outcome=rate_limited" in out, out
		if workflow_name in ("clarify.yml", "plan.yml", "orchestrate_clarify_respond.yml"):
			rc, out, gh_output = _run_refctx(workflow_name, "ok", "false", rate_limited)
			assert rc == 0 and "ref=\n" in gh_output, f"{workflow_name} rate_limited routing off\n{out}"


def test_plan_pr_not_open_refusal_keeps_other_reasons_fatal() -> None:
	"""Issue #7477: plan's refctx step exited 1 on reason=pr_not_open
	(multi-user-ai-agent#264, run 38862023746). It now succeeds with an
	empty ref and pr_not_open=true; any other exit-3 reason, including one
	that only starts with pr_not_open, still fails closed."""
	def refusal(reason: str) -> dict[str, str]:
		return {
			"FAKE_RESOLVER_RC": "3",
			"FAKE_RESOLVER_STDERR": f"::error::CI_TRIAGE_PR_BRANCH_ROUTING issue=5 pr=7 outcome=refused reason={reason}",
		}

	rc, out, gh_output = _run_refctx("plan.yml", "ok", "true", refusal("pr_not_open"))
	assert rc == 0, out
	assert "ref=\n" in gh_output and "pr_not_open=true\n" in gh_output, gh_output
	assert "::notice::CI_TRIAGE_PR_BRANCH_ROUTING" in out, out
	assert "::error::" not in out, out
	for reason in ("pr_not_open_extra", "branch_missing", "pr_cross_repository"):
		rc, out, gh_output = _run_refctx("plan.yml", "ok", "true", refusal(reason))
		assert rc != 0, f"{reason}\n{out}"
		assert "ref=" not in gh_output and "pr_not_open" not in gh_output, f"{reason}: {gh_output!r}"
		assert "::error::CI_TRIAGE_PR_BRANCH_ROUTING" in out, out
	rc, out, gh_output = _run_refctx("plan.yml", "ok", "true", {"FAKE_RESOLVER_RC": "0"})
	assert rc == 0 and "pr_not_open" not in gh_output, gh_output


def test_implement_pr_not_open_skips_without_a_ref() -> None:
	"""Issue #7540: implement's refctx step exited 1 on reason=pr_not_open
	(bitsafe.io#551, run 39002911584). It now sets SKIP_IMPLEMENT=true and
	exits 0 without writing a ref; every other exit-3 reason, including one
	that only starts with pr_not_open, still fails closed."""
	def refusal(reason: str) -> dict[str, str]:
		return {
			"FAKE_RESOLVER_RC": "3",
			"FAKE_RESOLVER_STDERR": f"::error::CI_TRIAGE_PR_BRANCH_ROUTING issue=5 pr=7 outcome=refused reason={reason}",
		}

	rc, out, gh_output, env_text = _run_refctx_full("implement.yml", "ok", "true", refusal("pr_not_open"))
	assert rc == 0, out
	assert "SKIP_IMPLEMENT=true" in env_text, env_text
	assert "ref=" not in gh_output, gh_output
	assert "pr_not_open=true\n" in gh_output and "pr_not_open_pr=7\n" in gh_output, gh_output
	assert "::error::" not in out, out
	for reason in ("pr_not_open_extra", "branch_missing", "pr_cross_repository"):
		rc, out, gh_output, env_text = _run_refctx_full("implement.yml", "ok", "true", refusal(reason))
		assert rc != 0, f"{reason}\n{out}"
		assert "SKIP_IMPLEMENT" not in env_text, f"{reason}: {env_text!r}"
		assert "ref=" not in gh_output and "pr_not_open" not in gh_output, f"{reason}: {gh_output!r}"
		assert "::error::CI_TRIAGE_PR_BRANCH_ROUTING" in out, out
	rc, out, gh_output, env_text = _run_refctx_full("implement.yml", "ok", "true", {"FAKE_RESOLVER_RC": "0"})
	assert rc == 0 and "ref=" in gh_output and "pr_not_open" not in gh_output, gh_output
	assert "SKIP_IMPLEMENT" not in env_text, env_text


CI_TRIAGE_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "integration_ref_resolver_ci_triage"


def test_implement_pr_not_open_fixtures_skip_without_a_ref() -> None:
	"""Issue #7540: drive implement's step with the refusal the real resolver
	emits for the merged and closed-unmerged pr_not_open fixtures."""
	resolver = REPO_ROOT / "scripts" / "resolve_integration_ref.sh"
	for name in ("pr_not_open_merged.json", "pr_not_open_closed.json"):
		fixture_path = CI_TRIAGE_FIXTURE_DIR / name
		fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
		assert fixture["expected_exit_code"] == 3 and fixture["expected_error_reason"] == "pr_not_open", name
		# The real resolver must still refuse this fixture with pr_not_open.
		self_test = subprocess.run(
			["bash", str(resolver), "--self-test-case", str(fixture_path)],
			capture_output=True, text=True, timeout=120,
		)
		assert self_test.returncode == 0, f"{name}\n{self_test.stdout}{self_test.stderr}"
		(issue_number,) = fixture["issues"].keys()
		(pr_number,) = fixture["pulls"].keys()
		refusal = (f"::error::CI_TRIAGE_PR_BRANCH_ROUTING issue={issue_number} pr={pr_number} "
			f"outcome=refused reason={fixture['expected_error_reason']}")
		rc, out, gh_output, env_text = _run_refctx_full("implement.yml", "ok", "true",
			{"FAKE_RESOLVER_RC": "3", "FAKE_RESOLVER_STDERR": refusal})
		assert rc == 0, f"{name}\n{out}"
		assert "SKIP_IMPLEMENT=true" in env_text, f"{name}: {env_text!r}"
		assert "ref=" not in gh_output, f"{name}: {gh_output!r}"
		assert "pr_not_open=true\n" in gh_output and f"pr_not_open_pr={pr_number}\n" in gh_output, gh_output
		assert "::error::" not in out, out


def test_implement_diagnose_step_skips_when_helper_not_staged() -> None:
	"""Issue #7540: a failure before support staging must not run the
	missing diagnose helper (bash exit 127)."""
	wf = _workflow_text("implement.yml")
	start = wf.index("      - name: Diagnose post-Codex failure and create fix-up issues")
	end = wf.index("\n      - name:", start + 1)
	step = wf[start:end]
	guard = ('if [ -z "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}" ] || '
		'[ ! -f "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/implement_diagnose_post_codex_failure.sh" ]; then')
	launch = 'bash "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/implement_diagnose_post_codex_failure.sh"'
	assert guard in step and launch in step
	assert step.index(guard) < step.index(launch)
	guarded = step[step.index(guard):step.index("\n          fi\n", step.index(guard))]
	assert "exit 0" in guarded and "::warning::" in guarded


_LABEL_MISSING_LINE = "::error::CI_TRIAGE_PR_BRANCH_ROUTING issue=5 pr=7 outcome=refused reason=label_missing"
_LABEL_MISSING_MARKER = "<!-- ai:integration-ref-label-missing:v1 issue=5 -->"


def _label_missing_env(line: str = _LABEL_MISSING_LINE, **extra: str) -> dict[str, str]:
	env = {"FAKE_RESOLVER_RC": "3", "FAKE_RESOLVER_STDERR": line, "FAKE_GH_LOGIN": "pipeline-bot"}
	env.update(extra)
	return env


def _posts(calls: list[list[str]]) -> list[list[str]]:
	return [c for c in calls if "POST" in c]


def _post_body(call: list[str]) -> str:
	for arg in call:
		if arg.startswith("body="):
			return arg[len("body="):]
	raise AssertionError(f"no body in {call!r}")


def test_clarify_label_missing_refusal_holds_issue() -> None:
	"""Issue #7516: clarify on #7515 failed every run on the deterministic
	reason=label_missing refusal and re-filed heal reports. It now holds the
	issue (one comment, ai:blocked, label_missing_hold=true) and exits 0,
	still without routing to the default branch."""
	for line in (_LABEL_MISSING_LINE, "::error::CI_TRIAGE_PR_BRANCH_ROUTING issue=5 pr=none outcome=refused reason=label_missing"):
		rc, out, gh_output, calls = _run_refctx_gh("clarify.yml", "ok", "true", _label_missing_env(line))
		assert rc == 0, out
		assert "label_missing_hold=true\n" in gh_output and "ref=\n" in gh_output, gh_output
		assert "::warning::CI_TRIAGE_PR_BRANCH_ROUTING issue=5" in out, out
		assert "::error::CI_TRIAGE" not in out, out
		assert "INTEGRATION_REF_RESOLVE outcome=held reason=label_missing issue=5 comment=posted label=applied" in out, out
		posts = _posts(calls)
		assert len(posts) == 2, calls
		assert posts[0][posts[0].index("POST") + 1] == "repos/consumer/repo/issues/5/comments", posts
		body = _post_body(posts[0])
		assert body.rstrip().endswith(_LABEL_MISSING_MARKER), body
		assert not any(l.lstrip().startswith("/reclarify") for l in body.splitlines()), body
		assert "ai:check-triage" in body and "`- Tracking issue: #N`" in body and "(untrusted)" not in body, body
		assert posts[1][posts[1].index("POST") + 1] == "repos/consumer/repo/issues/5/labels", posts
		assert "labels[]=ai:blocked" in posts[1], posts


def test_clarify_label_missing_hold_dedups_on_trusted_marker() -> None:
	trusted = '[[{"user":{"login":"pipeline-bot"},"body":"text\\n%s\\n"}]]' % _LABEL_MISSING_MARKER
	rc, out, gh_output, calls = _run_refctx_gh("clarify.yml", "ok", "true", _label_missing_env(FAKE_GH_COMMENTS_JSON=trusted))
	assert rc == 0 and "label_missing_hold=true" in gh_output, out
	posts = _posts(calls)
	assert len(posts) == 1 and "labels[]=ai:blocked" in posts[0], calls
	assert "comment=present" in out, out
	forged = '[[{"user":{"login":"someone-else"},"body":"%s"}]]' % _LABEL_MISSING_MARKER
	rc, out, gh_output, calls = _run_refctx_gh("clarify.yml", "ok", "true", _label_missing_env(FAKE_GH_COMMENTS_JSON=forged))
	assert rc == 0 and len(_posts(calls)) == 2, calls
	assert "comment=posted" in out, out


def test_clarify_label_missing_hold_fails_closed_on_write_errors() -> None:
	reasons = {
		"user": "identity_unavailable",
		"comments": "comments_unavailable",
		"comment_post": "comment_failed",
		"label_post": "label_failed",
	}
	for fail, reason in reasons.items():
		rc, out, gh_output, _calls = _run_refctx_gh("clarify.yml", "ok", "true", _label_missing_env(FAKE_GH_FAIL=fail))
		assert rc != 0, f"{fail}\n{out}"
		assert "label_missing_hold" not in gh_output and "ref=" not in gh_output, f"{fail}: {gh_output!r}"
		assert f"INTEGRATION_REF_RESOLVE outcome=hold_failed reason={reason} issue=5" in out, out
	rc, out, gh_output, _calls = _run_refctx_gh("clarify.yml", "ok", "true", _label_missing_env(FAKE_GH_COMMENTS_JSON="not json"))
	assert rc != 0 and "reason=comments_unavailable" in out, out


def test_label_missing_hold_is_clarify_only_and_exact() -> None:
	"""Every other exit-3 refusal still fails clarify, and no other phase
	holds: they all fail without a GitHub write."""
	others = (
		"::error::CI_TRIAGE_PR_BRANCH_ROUTING issue=5 pr=7 outcome=refused reason=label_missing_extra",
		"::error::CI_TRIAGE_PR_BRANCH_ROUTING issue=5 pr=7 outcome=refused reason=branch_missing",
		"::error::CI_TRIAGE_PR_BRANCH_ROUTING issue=6 pr=7 outcome=refused reason=label_missing",
		"note: outcome=refused reason=label_missing",
		"",
	)
	for line in others:
		rc, out, gh_output, calls = _run_refctx_gh("clarify.yml", "ok", "true", _label_missing_env(line))
		assert rc != 0, f"{line!r}\n{out}"
		assert "label_missing_hold" not in gh_output and calls == [], f"{line!r}: {calls!r}"
	for workflow_name in sorted(REQUIRED_RESOLVER_WORKFLOWS - {"clarify.yml"}):
		rc, out, gh_output, calls = _run_refctx_gh(workflow_name, "ok", "true", _label_missing_env())
		assert rc != 0 and "ref=" not in gh_output, f"{workflow_name}\n{out}"
		assert calls == [], f"{workflow_name}: {calls!r}"


# Steps after refctx that never run on the successful hold path (they need
# failure() / cancelled()) or that guard themselves.
_HOLD_GATE_FAILURE_STEPS = {
	"Record clarification run failed",
	"Comment on issue failure",
	"Queue failed reclarify for poller",
	"Telegram failure notification",
}
_HOLD_GATE_SELF_GUARDED_STEPS = {
	"Record GH_PAT budget at clarify end",
	"Report refused AI engine fallbacks",
}


def _clarify_job_steps() -> list[tuple[str, str]]:
	wf = _workflow_text("clarify.yml")
	job = wf[wf.index("\n  clarify:\n"):wf.index("\n  heal-report:")]
	chunks = job.split("\n      - name: ")[1:]
	return [(chunk.split("\n", 1)[0].strip(), chunk) for chunk in chunks]


def test_clarify_steps_after_refctx_honour_label_missing_hold() -> None:
	gate = "steps.refctx.outputs.label_missing_hold != 'true'"
	steps = _clarify_job_steps()
	names = [name for name, _ in steps]
	refctx = names.index("Resolve integration ref")
	assert len(steps) - refctx > 20, names
	for name, chunk in steps[refctx + 1:]:
		ifs = [l for l in chunk.splitlines() if l.startswith("        if: ")]
		cond = ifs[0] if ifs else ""
		if name in _HOLD_GATE_FAILURE_STEPS:
			assert "failure()" in cond or "cancelled()" in cond, name
		elif name in _HOLD_GATE_SELF_GUARDED_STEPS:
			continue
		else:
			assert gate in cond, f"{name} is not gated on label_missing_hold: {cond!r}"


def test_clarify_always_steps_tolerate_missing_setup() -> None:
	"""Run 38983115876: after the job stopped early, the always()/failure()
	steps failed on RUNTIME_DIR and a missing scripts/tg_helpers.sh."""
	for step_name in ("Semantic cache lookup", "Write run summary", "Telegram failure notification"):
		with tempfile.TemporaryDirectory() as tmp:
			tmp_path = Path(tmp)
			output = tmp_path / "github_output"
			summary = tmp_path / "summary"
			env = {
				"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
				"GITHUB_OUTPUT": str(output),
				"GITHUB_STEP_SUMMARY": str(summary),
				"ISSUE_NUMBER": "5",
			}
			res = subprocess.run(
				["bash", "-c", _step_run_body("clarify.yml", step_name)],
				env=env, cwd=tmp, capture_output=True, text=True, timeout=60,
			)
			out = res.stdout + res.stderr
			assert res.returncode == 0, f"{step_name}\n{out}"
			assert "::warning::" in out, f"{step_name}\n{out}"
			if step_name == "Semantic cache lookup":
				assert "cache_hit=false" in output.read_text(encoding="utf-8")


_RELEASE_FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_GH_LOG"
if [ "$1 $2" = "api user" ]; then echo "${FAKE_GH_LOGIN:-pipeline-bot}"; exit 0; fi
exit 0
"""


def _run_phase_label_step(labels: list[str], comments: list[dict[str, object]], login: str = "pipeline-bot") -> tuple[int, str, list[str]]:
	body = _step_run_body("clarify.yml", "Set clarification phase label").replace("a" * 40, "issue_comment")
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		(tmp_path / "scripts").mkdir()
		(tmp_path / "scripts" / "gh_helpers.sh").write_text('gh_retry() { "$@"; }\n', encoding="utf-8")
		(tmp_path / "scripts" / "label_helpers.sh").write_text("ensure_label_exists() { :; }\n", encoding="utf-8")
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		fake_gh = bin_dir / "gh"
		fake_gh.write_text(_RELEASE_FAKE_GH, encoding="utf-8")
		fake_gh.chmod(0o755)
		meta = tmp_path / "meta.json"
		meta.write_text(json.dumps({"labels": [{"name": n} for n in labels]}), encoding="utf-8")
		all_comments = tmp_path / "comments.json"
		all_comments.write_text(json.dumps(comments), encoding="utf-8")
		log = tmp_path / "gh.log"
		log.write_text("", encoding="utf-8")
		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
			"ISSUE_NUMBER": "5",
			"ISSUE_META_FILE": str(meta),
			"ISSUE_ALL_COMMENTS_FILE": str(all_comments),
			"FAKE_GH_LOG": str(log),
			"FAKE_GH_LOGIN": login,
		}
		res = subprocess.run(["bash", "-c", body], env=env, cwd=tmp, capture_output=True, text=True, timeout=60)
		return res.returncode, res.stdout + res.stderr, log.read_text(encoding="utf-8").splitlines()


def test_clarify_reclarify_releases_label_missing_hold_block() -> None:
	"""Issue #7516: after the operator repairs the labels and posts /reclarify,
	the hold's ai:blocked label is removed, but only for the GH_PAT login's
	hold marker. Plan keeps ai:blocked when ai:clarification is also present."""
	trusted = [{"id": 41, "user": {"login": "pipeline-bot"}, "body": f"text\n{_LABEL_MISSING_MARKER}\n"}]
	delete = "api -X DELETE repos/consumer/repo/issues/5/labels/ai:blocked"
	patch = "api -X PATCH repos/consumer/repo/issues/comments/41 -f body=text"
	released = "<!-- ai:integration-ref-label-missing:v1 issue=5 released -->"
	rc, out, calls = _run_phase_label_step(["ai:clarification", "ai:blocked"], trusted)
	assert rc == 0, out
	assert any(delete in c for c in calls), calls
	# The marker is retired before the label goes, so the release is one-shot.
	assert patch in calls and calls.index(patch) + 1 < len(calls) and calls[calls.index(patch) + 1] == released, calls
	assert calls.index(patch) < next(i for i, c in enumerate(calls) if delete in c), calls
	assert "INTEGRATION_REF_RESOLVE outcome=released reason=label_missing issue=5" in out, out
	forged = [{"id": 42, "user": {"login": "someone-else"}, "body": _LABEL_MISSING_MARKER}]
	# A hold marker already released never removes a later, unrelated ai:blocked.
	already_released = [{"id": 43, "user": {"login": "pipeline-bot"}, "body": f"text\n{released}"}]
	for labels, comments in ((["ai:blocked"], forged), (["ai:blocked"], []), (["ai:blocked"], already_released)):
		rc, out, calls = _run_phase_label_step(labels, comments)
		assert rc == 0, out
		assert not any(delete in c or "PATCH" in c for c in calls), (labels, calls)
	# ai:blocked already removed by hand: the open marker is still retired, so
	# it cannot release a later, unrelated ai:blocked; nothing is deleted.
	rc, out, calls = _run_phase_label_step(["ai:clarification"], trusted)
	assert rc == 0, out
	assert patch in calls and not any(delete in c for c in calls), calls
	assert "outcome=released reason=label_missing issue=5 label=absent" in out, out


def test_clarify_label_missing_hold_writes_nothing_for_untrusted_comment() -> None:
	"""The job predicate admits a PR-dependency release comment from any
	author; the hold must not write with GH_PAT for it before the route
	step's trust check."""
	untrusted = {"HOLD_EVENT_NAME": "issue_comment", "HOLD_COMMENT_LOGIN": "outsider", "HOLD_COMMENT_ASSOCIATION": "NONE"}
	rc, out, gh_output, calls = _run_refctx_gh("clarify.yml", "ok", "true", _label_missing_env(**untrusted))
	assert rc == 0, out
	assert "label_missing_hold=true\n" in gh_output, gh_output
	assert not _posts(calls), calls
	assert "reason=label_missing_untrusted_comment" in out, out
	for trusted in (
		{"HOLD_EVENT_NAME": "issue_comment", "HOLD_COMMENT_LOGIN": "outsider", "HOLD_COMMENT_ASSOCIATION": "MEMBER"},
		{"HOLD_EVENT_NAME": "issue_comment", "HOLD_COMMENT_LOGIN": "pipeline-bot", "HOLD_COMMENT_ASSOCIATION": "NONE"},
	):
		rc, out, gh_output, calls = _run_refctx_gh("clarify.yml", "ok", "true", _label_missing_env(**trusted))
		assert rc == 0 and len(_posts(calls)) == 2, (trusted, calls)


def main() -> int:
	test_checkout_workflows_are_all_classified()
	test_allowlist_entries_have_rationale()
	test_required_workflows_enforce_integration_ref_contract()
	test_required_workflows_fail_closed_on_ci_triage_routing_refusal()
	test_required_workflows_fail_closed_when_resolver_staging_fails()
	test_clarify_tolerates_pr_not_open_and_routing_fails_closed_on_rate_limit()
	test_plan_pr_not_open_refusal_keeps_other_reasons_fatal()
	test_implement_pr_not_open_skips_without_a_ref()
	test_implement_pr_not_open_fixtures_skip_without_a_ref()
	test_implement_diagnose_step_skips_when_helper_not_staged()
	test_clarify_label_missing_refusal_holds_issue()
	test_clarify_label_missing_hold_dedups_on_trusted_marker()
	test_clarify_label_missing_hold_fails_closed_on_write_errors()
	test_label_missing_hold_is_clarify_only_and_exact()
	test_clarify_steps_after_refctx_honour_label_missing_hold()
	test_clarify_always_steps_tolerate_missing_setup()
	test_clarify_reclarify_releases_label_missing_hold_block()
	test_clarify_label_missing_hold_writes_nothing_for_untrusted_comment()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
