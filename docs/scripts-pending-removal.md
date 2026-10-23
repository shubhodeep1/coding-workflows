# Scripts Pending Removal

Centralized registry of single-use scripts, long-running scripts, and
long-running supervisors that have a known future-removal condition.

Mandated by **§18.F** (`CLAUDE.md`) and **§20.F**
(`unattended_system_instructions.md`).

This is **one** living registry — do not create per-script removal
docs. Entries are added when a script is introduced and **removed**
from this file when the script itself is removed from the codebase.
Git history is the audit trail; there is no "removed" archive section.

## Entry Schema

Each entry MUST include all of the following fields. Use the per-script
block format below — tables collapse multi-line preflight checks.

- **Script path** — the script, supervisor entry point, or workflow
  file the entry is about (repo-relative path).
- **Introduced in** — PR number and date the script landed.
- **Type** — one of `single-use`, `long-running`, `supervisor`.
- **Removal trigger** — the concrete condition that makes removal
  safe. Examples:
  - `after backfill users_email_lowercase completes for all docs`
  - `when feature flag new_pricing_v2 is GA (100% rollout) for 30 days`
  - `when supervisor_v2 replaces supervisor_v1 in production`
  - `permanent — review annually` (use only when no sunset applies; do
    not omit the field)
- **Removal preflight checks** — explicit list of checks that MUST
  pass before the script is removed, to verify the script has already
  done its job. Each check names the exact command, query, or signal
  to inspect and the expected result / threshold. These checks protect
  against removing a script that hasn't finished its work.
- **Owner** — GitHub handle of the person / agent who owns the
  removal decision.

## Template

Copy this block when adding a new entry:

```markdown
### `path/to/script-or-workflow`

- **Introduced in:** #NNNN (YYYY-MM-DD)
- **Type:** single-use | long-running | supervisor
- **Removal trigger:** <concrete condition>
- **Removal preflight checks:**
  - `<exact command / query>` returns `<expected result>`.
  - `<signal to inspect>` shows `<expected threshold>`.
- **Owner:** @handle
```

## Entries

### `scripts/workflow_failure_heal_recovery_scan.sh` + the `recovery-candidates` / `recovery-scan` jobs and `schedule` trigger of `.github/workflows/workflow-failure-heal-intake.yml`

- **Introduced in:** PR for #7333 (2026-10-13)
- **Type:** single-use
- **Removal trigger:** no open, pipeline-authored `ai:workflow-heal` issue without a scope marker for 30 consecutive days. The intake's creation guards (#7059, #6871) already refuse markerless bodies, so no new ones are filed.
- **Removal preflight checks:**
  - The `recovery-candidates` job of `workflow-failure-heal-intake.yml` logs `heal_scope_recovery outcome=scan reason=listed count=0` on every run of the last 30 days (`gh run list --workflow workflow-failure-heal-intake.yml --event schedule -R shubhodeep1/coding-workflows`).
  - `gh issue list -R shubhodeep1/coding-workflows -l ai:workflow-heal -s open --json number,body --jq '[.[] | select(.body | contains("ai:workflow-heal-scope") | not)] | length'` prints `0`.
  - The `recovery-scan` job logs of the last 30 days contain no `heal_scope_recovery outcome=replaced` or `outcome=attempt_failed` line.
  - The removal PR also drops the `recovery_scan` origin and `REPORT_AUTH=reconstructed` from `scripts/workflow_failure_heal_intake.sh`, `heal-scope-recovery reconstruct` / `scan-marker` from `scripts/workflow_failure_heal.py`, `tests/test_workflow_failure_heal_recovery_scan.py` (and its `ci.yml` entry) and the `docs/INVENTORY.md` line.
- **Owner:** @shubhodeep1

### `scripts/sync_claude_live_copies.py` + `.github/workflows/sync-claude-live-copies.yml`

- **Introduced in:** #6118 (2026-10-05)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `gh api 'repos/shubhodeep1/coding-workflows/actions/workflows/sync-claude-live-copies.yml/runs?status=in_progress&per_page=1' --jq '.total_count'` returns `0` (no live-copy sync run is still active).
  - `gh api 'repos/shubhodeep1/coding-workflows/pulls?state=open&head=shubhodeep1:ai/sync-claude-live-copies&per_page=100'` returns `[]` (no unmerged live-copy sync PR).
  - `PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_claude_live_copies.py check` exits 0 on the default branch (no template/live drift at removal time).
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_claude_template_live_parity.py` passes (the parity guard remains effective or has a tested replacement).
  - `rg -n 'sync_claude_live_copies\.py' .github/workflows scripts --glob '!scripts/sync_claude_live_copies.py'` shows no remaining caller after a replacement for the push-to-main sync has been wired in and verified.
- **Owner:** @shubhodeep1

### `scripts/workflow_failure_heal_report.sh` + `scripts/workflow_failure_heal_autofix_report.sh` + `scripts/workflow_failure_heal_intake.sh` + `scripts/workflow_failure_heal.py` + `.github/workflows/workflow_failure_heal.yml` + `.github/workflows/workflow-failure-heal-intake.yml`

- **Introduced in:** #4165 (2026-09-20)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `gh workflow view workflow-failure-heal-intake.yml -R shubhodeep1/coding-workflows` confirms the `repository_dispatch` (`workflow-failure-heal`) and `workflow_run` entry points still exist and still run `scripts/workflow_failure_heal_intake.sh`.
  - `gh api "repos/shubhodeep1/coding-workflows/issues?state=open&labels=ai:workflow-heal"` returns `[]` (no heal issue is waiting on the pipeline), or a replacement heal path owns those issues.
  - `rg -n 'ai-workflow-failure-heal.yml' workflow-templates/profiles/full.txt agents.md` shows the consumer wrapper is no longer delivered, i.e. consumers have been moved off the report path first.
  - `rg -n 'workflow_failure_heal_autofix_report.sh' .github/workflows/review_autofix.yml scripts/stage_workflow_support.sh` shows the review/autofix failure reporter step and its staging entry have been removed first.
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_workflow_failure_heal.py` returns exit code 0.
- **Owner:** @shubhodeep1

### `scripts/workflow_failure_heal_queue.sh` + the "Wait for the heal intake queue" / "Leave the heal intake queue" steps of `.github/workflows/workflow-failure-heal-intake.yml`

- **Introduced in:** the PR "Queue heal intake reports instead of cancelling them" (2026-10-23)
- **Type:** long-running
- **Removal trigger:** permanent — review annually (or together with the heal intake in the entry above)
- **Removal preflight checks:**
  - `rg -n 'workflow_failure_heal_queue\.sh' .github/workflows` returns nothing, and both jobs of `workflow-failure-heal-intake.yml` use the shared `workflow-failure-heal-intake` concurrency group again (the `HEAL_INTAKE_QUEUE_ENABLED=false` behaviour).
  - `gh api "repos/shubhodeep1/coding-workflows/issues?state=open&labels=ai:heal-intake-queue"` returns at most the queue issue, and its comments hold no ticket younger than `HEAL_INTAKE_QUEUE_LEASE_SECONDS` (10800 s), so no run is waiting; then close that issue.
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_workflow_failure_heal.py` passes after `tests/test_workflow_failure_heal_queue.py` and the `heal-queue` subcommand are removed.
- **Owner:** @shubhodeep1

### `scripts/workflow_failure_heal_non_retryable_backfill.sh` + the "Backfill non-retryable autofix failure reports" step of `.github/workflows/review_autofix_sweep.yml` + the `non-retryable-backfill` job of `workflow-templates/ai-review.yml`

- **Introduced in:** the PR for #7291 (2026-10-14)
- **Type:** long-running
- **Removal trigger:** permanent — review annually (or together with the review/autofix reporter and heal intake in the entry above)
- **Removal preflight checks:**
  - The last 10 runs of `review_autofix_sweep.yml` log `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL outcome=complete ... dispatched=0` and no `pr=<n> outcome=skip reason=tick_cap` line (`gh run view <id> --log | grep WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL`), so no undiagnosed non-retryable failure is still waiting on the backfill.
  - The same runs log no `outcome=warn reason=listing_truncated` line, or every rotated window has been covered with `dispatched=0`.
  - `rg -n 'workflow_failure_heal_non_retryable_backfill\.sh' .github/workflows workflow-templates` shows the sweep step and the consumer job removed first, and consumers have synced an `ai-review.yml` without the job.
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_workflow_failure_heal.py` passes after the backfill selector and `tests/test_workflow_failure_heal_non_retryable_backfill.py` are removed.
- **Owner:** @shubhodeep1

### `scripts/promote_main_cycle.sh` + `scripts/apply_analysis_on_main.sh` + the `cycle` job of `.github/workflows/promote-main-to-stable.yml`

- **Introduced in:** #4134 (2026-09-19)
- **Type:** supervisor
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `gh workflow view promote-main-to-stable.yml -R shubhodeep1/coding-workflows` confirms the daily schedule still exists and the `cycle` job still runs `scripts/promote_main_cycle.sh`.
  - `gh api "repos/shubhodeep1/coding-workflows/issues?state=open&labels=ai:orchestrator-tracking,ai:comprehensive-test-pending"` returns `[]` (no proving or verifying run in flight).
  - `git ls-remote origin refs/heads/main 'refs/tags/stable^{}'` shows no code change on `main` since the tag, or a replacement promotion path is live.
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_promote_main_cycle.py tests/test_apply_analysis_on_main.py tests/test_orchestrate_poll_promote_cycle.py` returns exit code 0.
- **Owner:** @shubhodeep1

### `scripts/auto_release_stable.sh` + `.github/workflows/auto-release-stable.yml`

- **Introduced in:** #4134 (2026-09-19)
- **Type:** supervisor
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `gh workflow view auto-release-stable.yml -R shubhodeep1/coding-workflows` confirms the 6-hourly schedule still exists and still runs `scripts/auto_release_stable.sh`.
  - `git ls-remote origin refs/heads/stable 'refs/tags/stable^{}'` prints the same commit for both refs (nothing on the `stable` branch is waiting for a release).
  - `gh api "repos/shubhodeep1/coding-workflows/actions/workflows/test-and-mark-stable.yml/runs?per_page=1"` shows the latest run was not dispatched by the schedule, i.e. a replacement release path is live.
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_auto_release_stable.py` returns exit code 0.
- **Owner:** @shubhodeep1

### `scripts/propagate_consumer_secrets.sh` + `.github/workflows/propagate-consumer-secrets.yml`

- **Introduced in:** #6709 (2026-10-08); weekly only-missing schedule added in #6712 (2026-10-08)
- **Type:** supervisor
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `gh workflow view propagate-consumer-secrets.yml -R shubhodeep1/coding-workflows` shows the push trigger on `.github/ai/consumer_repos.json` and the weekly schedule.
  - For each entry in `.github/ai/consumer_repos.json`, `gh secret list -R <consumer> --json name` lists `CHECK_TRIAGE_ISSUES_TOKEN`, `GH_PAT`, `OPENROUTER_API_KEY` and `TG_BOT_SECRET`.
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_propagate_consumer_secrets.py` returns exit code 0.
- **Owner:** @shubhodeep1

### `scripts/workflow_retro.py`

- **Introduced in:** #3532 (2026-06-26)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `gh workflow view workflow-log-analysis.yml -R shubhodeep1/coding-workflows` confirms the scheduled retro path still exists and still invokes `scripts/workflow_retro.py`.
  - `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_workflow_retro.py` returns exit code 0 after the focused retro-script assertions pass.
  - `rg -n 'workflow_retro\.py' .github/workflows/workflow-log-analysis.yml tests/test_workflow_retro.py` shows the live scheduled caller plus its focused contract coverage.
- **Owner:** @shubhodeep1

### `scripts/validation_discovery_bootstrap.py` + `.github/workflows/validation-refresh.yml` discovery dispatch

- **Introduced in:** claude/eloquent-ramanujan-5Mud6 (2026-05-26)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `gh workflow view validation-refresh.yml -R shubhodeep1/coding-workflows` confirms the scheduled cron is still active and the workflow still references `scripts/validation_discovery_bootstrap.py`.
  - `python3 scripts/ai_memory.py validation-discovery get --repo <consumer> --enabled` returns a recent (within last 30 days) `success_*` entry for every consumer in `.github/ai/consumer_repos.json`, meaning every consumer has a committed `.ai/validate.yml` that discovery has audited at least once.
  - `gh pr list --repo <consumer> --label automation:validate-bootstrap --state open` returns an empty list for every consumer (no open discovery PRs pending reviewer action).
  - Removing this code path means daily discovery dispatch stops; ensure consumers' committed manifests are correct and stable before doing so.
- **Owner:** @shubhodeep1

### `scripts/render_prompt.sh`

- **Introduced in:** #3045 (2026-06-02)
- **Type:** single-use
- **Removal trigger:** when automated callers invoke `scripts/render_prompt.py` directly and no live workflow or script path still invokes `scripts/render_prompt.sh`.
- **Removal preflight checks:**
  - `rg -n 'render_prompt\.sh' .github/workflows scripts --glob '!scripts/render_prompt.sh'` returns no live caller matches.
  - `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_render_prompt_foundation.py` returns `OK: render prompt foundation assertions hold`.
  - `rg -n 'render_prompt\.py' .github/workflows scripts --glob '!scripts/render_prompt.sh'` shows the direct Python-renderer callers or staging references that replace the shim path.
- **Owner:** @shubhodeep1

### `scripts/stage_workflow_support.sh`

- **Introduced in:** codex/issue-3186 (2026-06-07); extended in #3211 (2026-06-07)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `rg -n 'stage_workflow_support\.sh' .github/workflows/review_autofix.yml .github/workflows/validate.yml` returns the shared helper invocation from both workflow staging steps.
  - `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_review_semble_contract.py` returns `OK: review_autofix Semble contract assertions hold`.
  - `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_validate_workflow_validate_bootstrap.py` returns exit code 0 after the validate bootstrap contract assertions pass.
  - `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_workflow_overlay_core.py` returns exit code 0 after the shared-helper overlay staging assertions pass.
  - `PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_workflow_script_refs.py` returns `All workflow script references resolve to existing files.`
- **Owner:** @shubhodeep1

### `scripts/review_collect_pr_metadata.sh`

- **Introduced in:** ai/issue-3203 (2026-06-07)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `rg -n 'review_collect_pr_metadata\.sh' .github/workflows/review_autofix.yml .github/workflows/internal-review.yml` shows the live bootstrap entry and the delegated workflow callsite.
  - `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_review_autofix_review_pipeline_contract.py` returns `OK: review_autofix review-pipeline plumbing contract holds`.
  - `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_test_and_mark_stable_alt_model_cleanup_pr_selector.py` returns `4 passed`.
- **Owner:** @shubhodeep1

### `scripts/check_failure_triage.sh` + `.github/workflows/check_failure_triage.yml`

- **Introduced in:** claude/llm-pr-check-resolution-7vjfvi (2026-06-28)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `rg -n 'check_failure_triage\.sh' .github/workflows/check_failure_triage.yml` shows the reusable workflow still stages and invokes the script.
  - `rg -n 'check_failure_triage\.yml' .github/workflows/internal-check-failure-triage.yml workflow-templates/ai-check-failure-triage.yml` shows both the self-hosting (`@main`) and consumer (`@stable`) wrappers still call the reusable workflow.
  - `PYTHONDONTWRITEBYTECODE=1 pytest -q tests/test_check_failure_triage_workflow_security.py` confirms trusted prompt staging, credential-free diagnosis, and fine-grained-token issue posting remain separated.
  - `PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_workflow_script_refs.py` returns `All workflow script references resolve to existing files.`
  - The feature is on by default, so removal requires confirming no consumer still wants it: for every consumer in `.github/ai/consumer_repos.json`, `gh variable list --repo <consumer>` shows `CHECK_FAILURE_TRIAGE_ENABLED` is explicitly `false` (the repo has opted out) before removing it.
- **Owner:** @shubhodeep1

### `.github/workflows/check-failure-triage-historical.yml`

- **Introduced in:** #7093 (2026-10-10)
- **Type:** single-use
- **Removal trigger:** the `ai:check-triage` issue for PR #6611's CI run 37922279826 (job 113792812346) is closed. The workflow already skips itself on every push once that issue exists; removing it also lets the historical inputs of `check_failure_triage.yml` stay unused until another historical follow-up needs them.
- **Removal preflight checks:**
  - `gh issue list --label ai:check-triage --search 37922279826 --state all -R shubhodeep1/coding-workflows` shows a closed issue whose body links run 37922279826.
  - `gh run list --workflow check-failure-triage-historical.yml --limit 5 -R shubhodeep1/coding-workflows` shows the latest runs ending in the guard job with `HISTORICAL_TRIAGE skip reason=already_filed` (no `repro` or `triage` job ran).
  - `rg -n 'check-failure-triage-historical' .github/ docs/ agents.md tests/` lists only this workflow, its inventory line, the generated TREE block, its entry in `tests/test_workflow_checkout_integration_ref_audit.py` and `tests/test_check_failure_triage_historical.py` (plus that test's line in `ci.yml`), which are removed together.
- **Owner:** @shubhodeep1

### `.github/workflows/workspace-cache-maintenance.yml`

- **Introduced in:** #3066 (2026-06-02)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `gh workflow view workspace-cache-maintenance.yml -R shubhodeep1/coding-workflows` confirms the workflow still exists and still has a scheduled nightly cron.
  - `gh run list --workflow workspace-cache-maintenance.yml --limit 5 -R shubhodeep1/coding-workflows` shows recent scheduled/manual runs completing with `success`.
- `gh cache list --repo shubhodeep1/coding-workflows --limit 100 --key workspace-v1- --sort created_at --order desc` still shows bounded workspace cache families (newest 3 retained per family) so the maintenance job remains operationally useful.
- **Owner:** @shubhodeep1

### `scripts/assemble_agents.py`

- **Introduced in:** claude/elegant-mendel-5pli2i-agents-fragments (2026-10-10)
- **Type:** long-running
- **Removal trigger:** permanent — review annually. The script is the only thing that folds `agents.d/` fragments into `agents.md` (this repo) and `AGENTS.md` (consumers); removing it without a replacement strands every fragment and hides the documentation it carries from the next release.
- **Removal preflight checks:**
  - `rg -n 'assemble_agents\.py' .github/workflows/ scripts/build_static_context.sh` returns no hits — no release step, consumer sync, CI check, staging list or static-context reader still uses it.
  - `ls -A agents.d/` shows only `README.md` in this repo, and the same holds for every repo in `.github/ai/consumer_repos.json` — no unassembled fragment would be stranded.
  - `rg -n 'agents\.d' CLAUDE.md prompts/mode-implement.txt prompts/_templates/mode-implement.txt` returns no hits — §30 and both implement prompts have been migrated, so agents are no longer told to write fragments nothing will fold.
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_assemble_agents.py tests/test_agents_fragment_contract.py` — both suites removed or migrated with the script.
- **Owner:** @shubhodeep1

### `scripts/assemble_changelog.py`

- **Introduced in:** claude/validate-consumer-issue-1zebie (2026-08-06)
- **Type:** long-running
- **Removal trigger:** permanent — review annually. The script is the only thing that folds `changelog.d/` fragments into `CHANGELOG.md`; removing it without a replacement strands every fragment and silently stops changelog assembly in this repo and in all 12 consumers listed in `.github/ai/consumer_repos.json`.
- **Removal preflight checks:**
  - `rg -n 'assemble_changelog\.py' .github/workflows/` returns no hits — no workflow (release-time in `mark-stable.yml` / `test-and-mark-stable.yml`, consumer-side in `update_workflows.yml`) still invokes it.
  - `ls -A changelog.d/` shows only `.gitkeep` in this repo, and the same holds for every repo in `.github/ai/consumer_repos.json` — no unassembled fragment would be stranded by the removal.
  - `rg -n 'changelog\.d' CLAUDE.md prompts/mode-implement.txt prompts/_templates/mode-implement.txt` returns no hits — §20 and both implement prompts have been migrated to whatever replaces the fragment workflow, so agents are no longer told to write fragments nothing will fold.
  - `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_assemble_changelog.py && PYTHONDONTWRITEBYTECODE=1 python3 tests/test_changelog_fragment_contract.py` — both suites removed or migrated alongside the script rather than left asserting a mechanism that no longer exists.
- **Owner:** @shubhodeep1

### `.github/workflows/opencode-live-smoke.yml`

- **Introduced in:** #3858 (2026-08-27)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `rg -n 'opencode-live-smoke\.yml' .github/workflows workflow-templates scripts --glob '!.github/workflows/opencode-live-smoke.yml'` returns no matches.
  - `rg -n 'opencode|OPENCODE_' .github/workflows .github/actions scripts` confirms no remaining OpenCode runtime references before the workflow is removed.
- **Owner:** @shubhodeep1

### `scripts/opencode_helpers.sh`

- **Introduced in:** #3858 (2026-08-27)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `rg -n 'opencode_helpers\.sh' .github/workflows scripts --glob '!scripts/opencode_helpers.sh'` returns no matches.
  - `rg -n 'opencode|OPENCODE_' .github/workflows .github/actions scripts` confirms no remaining OpenCode runtime references before the helper is removed.
- **Owner:** @shubhodeep1

### `scripts/write_opencode_config.sh`

- **Introduced in:** #3858 (2026-08-27)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `rg -n 'write_opencode_config\.sh' .github/workflows scripts --glob '!scripts/write_opencode_config.sh'` returns no matches.
  - `rg -n 'opencode|OPENCODE_' .github/workflows .github/actions scripts` confirms no remaining OpenCode runtime references before the helper is removed.
- **Owner:** @shubhodeep1

### `scripts/ai_engine_fallback_report.sh`

- **Introduced in:** #6705 (2026-10-07), plan item 3e of `docs/plans/unattended-claude-pipeline-completion-plan.md` (Refs #6664)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `git grep -n ai_engine_fallback_report.sh .github/workflows` returns no callers.
  - `git grep -n AI_ENGINE_FALLBACK_POLICY scripts/ai_engine.sh` returns no matches (the fallback policy was removed).
  - `gh issue list --repo shubhodeep1/coding-workflows --label ai:workflow-heal --state open --search engine_fallback_refused` returns no issues.
- **Owner:** @shubhodeep1

### `workflow-templates/.claude/hooks/prepush_affected_tests.py`

- **Introduced in:** PR for #7137 (2026-10-11)
- **Type:** long-running
- **Removal trigger:** permanent — review annually. It is the pre-push affected-test gate: the interactive `PreToolUse` hook and stamp, and (from plan phases P3-P5) every AI push path run it. Removing it drops the only pre-push test run.
- **Removal preflight checks:**
  - `grep -rn prepush_affected_tests .github/workflows scripts workflow-templates` returns no callers other than this file and `scripts/prepush_test_gate_run.sh`.
  - `workflow-templates/.claude/settings.json` and `.claude/settings.json` no longer register the `prepush_affected_tests.py hook` command, in this repo and in every repo in `.github/ai/consumer_repos.json`.
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_prepush_affected_tests.py` is removed or migrated with the module.
- **Owner:** @shubhodeep1

### `scripts/prepush_test_gate_run.sh`

- **Introduced in:** PR for #7137 (2026-10-11)
- **Type:** long-running
- **Removal trigger:** permanent — review annually. It is the only credential-free container path for running PR tests before an AI push (plan phases P3-P5 call it).
- **Removal preflight checks:**
  - `grep -rn prepush_test_gate_run .github/workflows scripts workflow-templates` returns no callers.
  - `docs/INVENTORY.md` row and the `tests/test_prepush_affected_tests.py` wrapper tests are removed in the same PR.
- **Owner:** @shubhodeep1

### `.github/workflows/pr6576-shard-followup.yml` + `scripts/pr6576_shard_followup.py` + `tests/test_pr6576_shard_followup.py`

- **Introduced in:** issue #7072 (2026-10-10); PR number assigned when the implementation PR opens.
- **Type:** single-use
- **Removal trigger:** the marker comment `<!-- ai:pr6576-shard-followup:v1 -->` is posted on #7072 by the GH_PAT account, and any defect issue it filed is closed.
- **Removal preflight checks:**
  - `gh api --paginate repos/shubhodeep1/coding-workflows/issues/7072/comments --jq '.[] | select(.body | contains("ai:pr6576-shard-followup:v1")) | .user.login'` prints the pipeline (GH_PAT) login.
  - `gh issue list -R shubhodeep1/coding-workflows --state open --search "ai:pr6576-shard-followup-defect in:body"` returns no issues.
  - `gh api 'repos/shubhodeep1/coding-workflows/actions/workflows/pr6576-shard-followup.yml/runs?status=in_progress&per_page=1' --jq .total_count` returns `0`.
- **Owner:** @shubhodeep1

### `scripts/consumer_health_check.sh` + `scripts/consumer_health_check.py` (daily step in `.github/workflows/orchestrate_poll.yml`)

- **Introduced in:** #7415 (2026-10-15)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `gh api 'repos/shubhodeep1/coding-workflows/issues?state=open&labels=ai%3Aconsumer-health&per_page=100' --jq length` returns `0` (no consumer problem is still being tracked), or a replacement check owns those issues.
  - `rg -n 'consumer_health_check' .github/workflows scripts --glob '!scripts/consumer_health_check.*'` lists only the four `orchestrate_poll.yml` steps, so they are removed together with the scripts.
  - A replacement that watches the same four signals (inactivity-disabled wrappers, manually disabled wrappers, silent pollers, tracking issues near the comment cap) is wired into the scheduler and has run green for 7 days.
- **Owner:** @shubhodeep1
