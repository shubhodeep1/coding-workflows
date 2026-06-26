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

### `.github/workflows/workspace-cache-maintenance.yml`

- **Introduced in:** #3066 (2026-06-02)
- **Type:** long-running
- **Removal trigger:** permanent — review annually
- **Removal preflight checks:**
  - `gh workflow view workspace-cache-maintenance.yml -R shubhodeep1/coding-workflows` confirms the workflow still exists and still has a scheduled nightly cron.
  - `gh run list --workflow workspace-cache-maintenance.yml --limit 5 -R shubhodeep1/coding-workflows` shows recent scheduled/manual runs completing with `success`.
- `gh cache list --repo shubhodeep1/coding-workflows --limit 100 --key workspace-v1- --sort created_at --order desc` still shows bounded workspace cache families (newest 3 retained per family) so the maintenance job remains operationally useful.
- **Owner:** @shubhodeep1
