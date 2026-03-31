# coding-workflows

Centralized reusable GitHub Actions workflows for AI-powered issue-to-PR automation.

## Overview

This repository contains reusable `workflow_call` workflows that power the full AI development pipeline:

1. **Clarify** — Determines if an issue has enough information, asks clarification questions if needed
2. **Plan** — Generates a structured implementation plan from clarified issues
3. **Implement** — Executes the approved plan and creates a pull request
4. **Review & Autofix** — Multi-model PR review with automated fix application
5. **Issue PR Status** — Syncs issue labels when PRs are merged/closed
6. **Cancel on PR Close** — Cancels orphaned workflow runs when PRs close
7. **Memory Maintenance** — Monthly compaction and archival of AI memory records

## Quickstart

Get AI-powered issue-to-PR automation running in your repository in a few minutes.

### 1. Add secrets and variables

In your consumer repository, go to **Settings → Secrets and variables → Actions** and configure:

#### Secrets

| Secret | Required | Used By | Description |
|---|---|---|---|
| `GH_PAT` | **Yes** | All workflows | GitHub Personal Access Token with `repo` scope |
| `OPENROUTER_API_KEY` | **Yes** | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond | [OpenRouter](https://openrouter.ai) API key for LLM access |
| `TG_BOT_SECRET` | No | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond | Telegram bot token for notifications |

#### Variables

| Variable | Required | Default | Used By | Description |
|---|---|---|---|---|
| `WORKFLOW_EDITOR_MODEL` | No | `openai/gpt-5.3-codex` | clarify, plan, implement, review_autofix | Model for code editing tasks |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | No | `true` | plan | Auto-trigger implementation when plan is clear |
| `ALLOW_WORKFLOW_EDITS` | No | `false` | review_autofix | Allow AI edits to `.github/workflows` files |
| `ENABLE_AUTO_MERGE` | No | `false` | review_autofix, orchestrate_poll | Auto-merge PRs (squash) when review passes. Requires "Allow auto-merge" in repo settings. |
| `MAX_AUTOFIX_ITERATIONS` | No | `3` | review_autofix | Maximum consecutive autofix rounds before the review loop stops and marks the PR ready to merge. |
| `TG_ADMIN_CHAT_ID` | No | — | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll | Telegram chat ID for notifications (pair with `TG_BOT_SECRET`) |
| `SERENA_VERSION` | No | `main` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll | Version/branch of the Serena MCP server |
| `SERENA_LANGUAGES` | No | `""` (empty) | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll | Languages for Serena symbol analysis |
| `SERENA_DISABLED` | No | `false` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll | Disable the Serena MCP server |
| `WORKFLOW_ORCHESTRATE_MODEL` | No | (falls back to `WORKFLOW_EDITOR_MODEL`) | orchestrate, orchestrate_poll | Model override for orchestrator decomposer and judge |
| `ORCHESTRATE_POLL_INTERVAL` | No | `10` | orchestrate | Reserved poll interval setting (current poll cadence is controlled by the poller wrapper cron schedule) |

**Thinking levels** — control the model's reasoning effort per phase. Valid values: `xhigh`, `high`, `medium`, `low`. Defaults are tuned per phase: `medium` for clarify (gap analysis doesn't need deep reasoning), `xhigh` for plan (architectural decisions benefit from maximum reasoning), `high` for implement (follows an existing plan), and `xhigh` for review (last line of defense for catching bugs).

| Variable | Default | Used By | Description |
|---|---|---|---|
| `THINKING_LEVEL_CLARIFY` | `medium` | clarify | Reasoning effort for the clarification phase |
| `THINKING_LEVEL_PLAN` | `xhigh` | plan | Reasoning effort for the planning phase |
| `THINKING_LEVEL_IMPLEMENT` | `xhigh` | implement | Reasoning effort for the implementation phase |
| `THINKING_LEVEL_REVIEWER` | `xhigh` | review_autofix | Reasoning effort for the reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `high` | review_autofix | Reasoning effort for the editor model (applying fixes) |
| `THINKING_LEVEL_ORCHESTRATE` | `xhigh` | orchestrate | Reasoning effort for project decomposition |
| `THINKING_LEVEL_JUDGE` | `xhigh` | orchestrate_poll | Reasoning effort for judge evaluation |
| `THINKING_LEVEL_CLARIFY_RESPOND` | `medium` | orchestrate_clarify_respond | Reasoning effort for auto-answering clarification questions |

**Tool call budgets** — soft limits on the number of MCP + shell tool calls per phase. The LLM treats these as guidelines; it may exceed them for large refactors that span many files.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `TOOL_CALL_BUDGET_CLARIFY` | `15` | clarify | Tool call budget for the clarification phase |
| `TOOL_CALL_BUDGET_PLAN` | `40` | plan | Tool call budget for the planning phase |
| `TOOL_CALL_BUDGET_IMPLEMENT` | `50` | implement | Tool call budget for the implementation phase |
| `TOOL_CALL_BUDGET_ORCHESTRATE` | `40` | orchestrate | Tool call budget for the decomposer |
| `TOOL_CALL_BUDGET_JUDGE` | `60` | orchestrate_poll | Tool call budget for the judge (needs deep repo inspection) |
| `TOOL_CALL_BUDGET_CLARIFY_RESPOND` | `15` | orchestrate_clarify_respond | Tool call budget for auto-answering clarification questions |

**Token warning thresholds** — when a phase exceeds this many tokens, a warning appears in the GitHub Actions run summary. Raise these for large repos where deeper exploration is expected.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `TOKEN_WARN_THRESHOLD_CLARIFY` | `80000` | clarify | Token usage warning threshold for clarification |
| `TOKEN_WARN_THRESHOLD_PLAN` | `200000` | plan | Token usage warning threshold for planning |
| `TOKEN_WARN_THRESHOLD_IMPLEMENT` | `200000` | implement | Token usage warning threshold for implementation |
| `TOKEN_WARN_THRESHOLD_ORCHESTRATE` | `200000` | orchestrate | Token usage warning threshold for orchestration |
| `TOKEN_WARN_THRESHOLD_CLARIFY_RESPOND` | `80000` | orchestrate_clarify_respond | Token usage warning threshold for auto-answering clarification questions |

### 2. Create wrapper workflows

Copy the ready-to-use templates from [`workflow-templates/`](workflow-templates/) into your repo's `.github/workflows/` directory. Reference implementations also live in [`.github/workflows/internal-*.yml`](.github/workflows/) in this repository.

At minimum, create these three core wrappers:

**`.github/workflows/ai-clarify.yml`** — Triages new issues automatically
```yaml
name: AI Clarify
on:
  issues:
    types: [opened]
  issue_comment:
    types: [created]
permissions:
  contents: read
  issues: write
jobs:
  clarify:
    uses: shubhodeep1/coding-workflows/.github/workflows/clarify.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-plan.yml`** — Generates an implementation plan when you comment `/answer`
```yaml
name: AI Plan
on:
  issue_comment:
    types: [created]
permissions:
  contents: read
  issues: write
jobs:
  plan:
    uses: shubhodeep1/coding-workflows/.github/workflows/plan.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-implement.yml`** — Executes the plan and opens a PR when you comment `/approved`
```yaml
name: AI Implement
on:
  issue_comment:
    types: [created]
permissions:
  contents: write
  issues: write
  pull-requests: write
jobs:
  implement:
    uses: shubhodeep1/coding-workflows/.github/workflows/implement.yml@stable
    secrets: inherit
```

#### Optional wrappers

**`.github/workflows/ai-review.yml`** — Multi-model PR review with automated fixes
```yaml
name: AI Review
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions:
  contents: write
  pull-requests: write
  issues: write
jobs:
  check-skip:
    runs-on: ubuntu-latest
    outputs:
      should_skip: ${{ steps.detect.outputs.should_skip }}
    steps:
      - name: Detect autofix commit
        id: detect
        env:
          GH_TOKEN: ${{ secrets.GH_PAT || github.token }}
          PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}
        run: |
          set -euo pipefail
          HEAD_MSG="$(gh api "repos/${{ github.repository }}/git/commits/${PR_HEAD_SHA}" --jq '.message' 2>/dev/null | head -1 || true)"
          if echo "${HEAD_MSG}" | grep -q '^\[ai-autofix\]'; then
            echo "should_skip=true" >> "$GITHUB_OUTPUT"
          else
            echo "should_skip=false" >> "$GITHUB_OUTPUT"
          fi

  review:
    needs: [check-skip]
    if: needs.check-skip.outputs.should_skip != 'true'
    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable
    with:
      allow_workflow_edits: ${{ vars.ALLOW_WORKFLOW_EDITS == 'true' }}
    secrets: inherit

  post-autofix:
    needs: [check-skip]
    if: needs.check-skip.outputs.should_skip == 'true'
    runs-on: ubuntu-latest
    steps:
      - name: Mark linked issues ready to merge
        env:
          GH_TOKEN: ${{ secrets.GH_PAT }}
        run: |
          # See workflow-templates/ai-review.yml for full implementation
          echo "Autofix commit detected — marking issues ready to merge"
```

> **Note:** The `check-skip` job uses the GitHub API to read the actual PR
> head commit message (via `github.event.pull_request.head.sha`). On
> `pull_request` events, `actions/checkout` checks out a merge commit whose
> message never starts with `[ai-autofix]`, so reading `git log -1 HEAD`
> would always miss autofix commits. See
> [`workflow-templates/ai-review.yml`](workflow-templates/ai-review.yml) for
> the full ready-to-copy template including the `post-autofix` job.

> **Warning — do NOT add a top-level `concurrency` block to this wrapper.**
> The reusable workflow already manages concurrency at the job level. Adding a
> workflow-level `concurrency` group with the same key (e.g.
> `pr-autofix-${{ github.event.pull_request.number }}`) causes a deadlock:
> the caller holds the lock while the called job waits for it, and GitHub
> Actions cancels the run. If you need to customize the concurrency group,
> do so only inside the reusable workflow, not in the caller.

**`.github/workflows/ai-issue-pr-status.yml`** — Syncs issue labels when PRs are merged/closed
```yaml
name: AI Issue PR Status
on:
  pull_request:
    types: [closed]
permissions:
  issues: write
jobs:
  status:
    uses: shubhodeep1/coding-workflows/.github/workflows/issue_pr_status.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-cancel-on-pr-close.yml`** — Cancels orphaned workflow runs when PRs close
```yaml
name: AI Cancel on PR Close
on:
  pull_request:
    types: [closed]
permissions:
  actions: write
jobs:
  cancel:
    uses: shubhodeep1/coding-workflows/.github/workflows/cancel_on_pr_close.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-memory-maintenance.yml`** — Monthly compaction and archival of AI memory
```yaml
name: AI Memory Maintenance
on:
  schedule:
    - cron: '0 3 1 * *'
permissions:
  contents: write
jobs:
  maintenance:
    uses: shubhodeep1/coding-workflows/.github/workflows/memory_maintenance.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-orchestrate.yml`** — Decomposes a project description into issues with a dependency DAG
```yaml
name: AI Orchestrate
on:
  workflow_dispatch:
    inputs:
      project_description:
        description: >
          Full project description. The orchestrator will decompose it into
          issues with a dependency DAG and dispatch them through the AI pipeline.
        required: true
        type: string
permissions:
  contents: write
  issues: write
  pull-requests: write
jobs:
  orchestrate:
    uses: shubhodeep1/coding-workflows/.github/workflows/orchestrate.yml@stable
    with:
      project_description: ${{ inputs.project_description }}
    secrets: inherit
```

**`.github/workflows/ai-orchestrate-clarify-respond.yml`** — Auto-answers clarification questions on orchestrator-managed issues
```yaml
name: AI Orchestrate Clarify Respond
on:
  issue_comment:
    types: [created]
permissions:
  contents: read
  issues: write
jobs:
  respond:
    uses: shubhodeep1/coding-workflows/.github/workflows/orchestrate_clarify_respond.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-orchestrate-poll.yml`** — Polls orchestrator progress, runs judge, dispatches next waves
```yaml
name: AI Orchestrate Poller
on:
  schedule:
    - cron: '*/10 * * * *'
permissions:
  contents: write
  issues: write
  pull-requests: write
  actions: write
jobs:
  poll:
    uses: shubhodeep1/coding-workflows/.github/workflows/orchestrate_poll.yml@stable
    secrets: inherit
```

> All internal wrapper reference implementations can be found in [`.github/workflows/internal-*.yml`](.github/workflows/).

### 3. Open an issue

Create a new issue describing a feature or bug fix. The pipeline kicks off automatically:

1. **Clarify** evaluates whether the issue has enough detail. If not, it comments with clarification questions.
2. Once the issue is clear, comment `/answer` to trigger **Plan** generation.
3. Review the plan, then comment `/approved` to start **Implementation** — a PR is created for you.

## Usage

Consumer repositories use thin wrapper workflows that call these reusable workflows:

```yaml
# .github/workflows/ai-clarify.yml
name: AI Clarify

on:
  issues:
    types: [opened]
  issue_comment:
    types: [created]

permissions:
  contents: read
  issues: write

jobs:
  clarify:
    uses: shubhodeep1/coding-workflows/.github/workflows/clarify.yml@stable
    secrets: inherit
```

See [`workflow-templates/`](workflow-templates/) in this repository for ready-to-copy caller wrappers.

## Reusable Workflows

| Workflow | Trigger (in consumer) | Description |
|---|---|---|
| `clarify.yml` | `issues.opened`, `issue_comment.created` | Issue clarity detection |
| `plan.yml` | `issue_comment.created` (`/answer`) | Implementation plan generation |
| `implement.yml` | `issue_comment.created` (`/approved`) | Plan execution + PR creation |
| `review_autofix.yml` | `pull_request.*` | Multi-model review + autofix |
| `issue_pr_status.yml` | `pull_request.closed` | Label/state sync |
| `cancel_on_pr_close.yml` | `pull_request.closed` | Active-run cancellation |
| `memory_maintenance.yml` | `schedule` (monthly) | Memory compaction/archival |
| `orchestrate.yml` | `workflow_dispatch` | Project decomposition + multi-issue orchestration |
| `orchestrate_clarify_respond.yml` | `issue_comment.created` | Auto-answers clarification questions on orchestrator issues |
| `orchestrate_poll.yml` | `schedule` (every ~10 min) | Orchestrator progress poller + judge + auto-recovery |

## Required Secrets

| Secret | Used By | Description |
|---|---|---|
| `GH_PAT` | All workflows | GitHub PAT with repo access |
| `OPENROUTER_API_KEY` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond | OpenRouter API key for LLM access |
| `TG_BOT_SECRET` | clarify, plan, implement, review_autofix, orchestrate, orchestrate_poll, orchestrate_clarify_respond | Telegram bot token (optional) |

## Required Variables

| Variable | Default | Description |
|---|---|---|
| `WORKFLOW_EDITOR_MODEL` | `openai/gpt-5.3-codex` | Model for code editing tasks |
| `TG_ADMIN_CHAT_ID` | — | Telegram chat ID for notifications |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | `true` | Auto-approve clear plans |
| `ALLOW_WORKFLOW_EDITS` | `false` | Allow AI edits to workflow files |
| `ENABLE_AUTO_MERGE` | `false` | Auto-merge PRs (squash) when review passes and checks are green |
| `MAX_AUTOFIX_ITERATIONS` | `3` | Maximum consecutive autofix rounds before stopping |
| `AI_MEMORY_BRANCH` | `ai-memory` | Branch used for persistent AI memory |
| `AI_MEMORY_ROOT` | `ai-memory` | Memory root path used by workflows |
| `AI_MEMORY_RETRIEVAL_PROFILES` | `ai-memory/config/retrieval_profiles.v1.json` | Retrieval role config |
| `AI_MEMORY_ENABLED` | `true` | Enable/disable memory operations |
| `THINKING_LEVEL_CLARIFY` | `medium` | Reasoning effort for clarification (`xhigh`, `high`, `medium`, `low`) |
| `THINKING_LEVEL_PLAN` | `xhigh` | Reasoning effort for planning |
| `THINKING_LEVEL_IMPLEMENT` | `xhigh` | Reasoning effort for implementation |
| `THINKING_LEVEL_REVIEWER` | `xhigh` | Reasoning effort for reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `high` | Reasoning effort for editor model (applying fixes) |
| `TOOL_CALL_BUDGET_CLARIFY` | `15` | Tool call budget for clarification |
| `TOOL_CALL_BUDGET_PLAN` | `40` | Tool call budget for planning |
| `TOOL_CALL_BUDGET_IMPLEMENT` | `50` | Tool call budget for implementation |
| `TOKEN_WARN_THRESHOLD_CLARIFY` | `80000` | Token warning threshold for clarification |
| `TOKEN_WARN_THRESHOLD_PLAN` | `200000` | Token warning threshold for planning |
| `TOKEN_WARN_THRESHOLD_IMPLEMENT` | `200000` | Token warning threshold for implementation |
| `WORKFLOW_ORCHESTRATE_MODEL` | (falls back to `WORKFLOW_EDITOR_MODEL`) | Model override for orchestrator/judge |
| `THINKING_LEVEL_ORCHESTRATE` | `xhigh` | Reasoning effort for project decomposition |
| `THINKING_LEVEL_JUDGE` | `xhigh` | Reasoning effort for judge evaluation |
| `ORCHESTRATE_POLL_INTERVAL` | `10` | Reserved poll interval setting (current poll cadence is controlled by the poller wrapper cron schedule) |
| `TOOL_CALL_BUDGET_ORCHESTRATE` | `40` | Tool call budget for decomposer |
| `TOOL_CALL_BUDGET_JUDGE` | `60` | Tool call budget for judge (needs deep repo inspection) |
| `TOKEN_WARN_THRESHOLD_ORCHESTRATE` | `200000` | Token warning threshold for orchestration |
| `THINKING_LEVEL_CLARIFY_RESPOND` | `medium` | Reasoning effort for auto-answering clarification questions |
| `TOOL_CALL_BUDGET_CLARIFY_RESPOND` | `15` | Tool call budget for auto-answering clarification questions |
| `TOKEN_WARN_THRESHOLD_CLARIFY_RESPOND` | `80000` | Token warning threshold for auto-answering clarification questions |

## Project Orchestrator

The orchestrator enables complex, multi-issue projects from a single prompt. It decomposes a project description into a dependency-aware DAG of GitHub issues, dispatches them through the existing AI pipeline in waves, and uses a judge to validate results between waves.

### Architecture

```
workflow_dispatch (project description)
    → Decomposer (LLM): breaks project into issues + dependency DAG
    → Creates tracking issue + child issues
    → Wave 1 issues enter pipeline (clarify → auto-answer → plan → implement → review → merge)
    → Poller (scheduled): monitors progress, dispatches next waves
    → Judge (LLM, xhigh thinking, full repo checkout): evaluates after each wave
        → complete: close tracking issue
        → in_progress: create fix-up issues, advance to next wave
        → failed: auto-recovery (revert + re-plan, retry once), then stop
```

### Setup

**1.** Copy the three wrapper workflows from [`workflow-templates/`](workflow-templates/) into your consumer repo's `.github/workflows/` directory:

- [`ai-orchestrate.yml`](workflow-templates/ai-orchestrate.yml) — triggers decomposition via `workflow_dispatch`
- [`ai-orchestrate-clarify-respond.yml`](workflow-templates/ai-orchestrate-clarify-respond.yml) — auto-answers clarification questions on orchestrator issues
- [`ai-orchestrate-poll.yml`](workflow-templates/ai-orchestrate-poll.yml) — scheduled poller (every 10 min)

Or create them manually — see the inline examples in the [Quickstart](#quickstart) section above.

**2.** Ensure your repo has the required secrets (`GH_PAT`, `OPENROUTER_API_KEY`) and optionally configure the orchestrator variables listed in [Required Variables](#required-variables).

**3.** Go to **Actions → AI Orchestrate → Run workflow**, paste your project description, and click **Run workflow**.

### How it works

1. **Decomposition:** The LLM reads your repo, breaks the project into scoped issues with a dependency graph, and creates a tracking issue (labeled `ai:orchestrator-tracking`).
2. **Wave dispatch:** Wave 1 issues (no dependencies) are created immediately and enter the existing clarify → plan → implement → review pipeline automatically. If clarification questions are raised, the `orchestrate_clarify_respond` workflow answers them automatically using an LLM, so the pipeline runs fully unattended.
3. **Auto-merge:** The poller automatically merges PRs via squash merge when they reach `ai:ready-to-merge`. This requires either (a) no branch protection rules, or (b) branch protection with "Require status checks" that have already passed. See [Enabling auto-merge](#enabling-auto-merge) below.
4. **Polling:** Every 10 minutes, the poller checks if the current wave's issues have reached `ai:merged`. When all are merged, it runs the judge.
5. **Judge:** Full repo checkout + tool access (Serena, shell, file reads) with `xhigh` thinking. Compares merged code against the project spec. Decides: complete, in_progress (next wave or fix-ups), or failed.
6. **Next wave:** When the judge approves, the poller creates the next wave's issues (deferred creation — they don't exist until their dependencies are met). This triggers `clarify.yml` via `issues.opened`.
7. **Auto-recovery:** On failure, the judge can revert problematic PRs and create fix-up issues. Recovery is attempted once; if it still fails, the project stops and the operator is notified via Telegram.
8. **Completion:** When the judge says "complete", the tracking issue is closed.

### Enabling auto-merge

Auto-merge works at two levels: the **review workflow** (merges individual PRs right after review passes) and the **orchestrator poller** (merges PRs for orchestrator-managed issues). Both use the same `ENABLE_AUTO_MERGE` variable.

**Step 1: Enable in GitHub repo settings**
1. Go to **Settings → General → Pull Requests** and check **Allow auto-merge**.
2. If you use branch protection, ensure your required status checks are configured (the PR will auto-merge once they pass).

**Step 2: Set the variable**
In **Settings → Secrets and variables → Actions → Variables**, add:
- `ENABLE_AUTO_MERGE` = `true`

This enables `gh pr merge --squash --auto` in both `review_autofix.yml` (right after setting `ai:ready-to-merge`) and the orchestrator poller. GitHub queues the merge and executes it once all required checks pass.

**Branch protection compatibility:**

| Setup | Auto-merge behavior |
|---|---|
| No branch protection | PR merged immediately after review passes |
| Branch protection + required checks | PR queued, merged once checks pass |
| Branch protection + required human reviews | Cannot auto-merge unless bot account is added as a bypass actor |

Your `GH_PAT` must have permission to enable auto-merge (repo scope with admin or write access).

### Labels

The orchestrator uses `ai:orchestrator-tracking` for tracking issues. Child issues use the standard `ai:*` phase labels. The `ai:orchestrator-tracking` label is defined in the [label contract](/.github/ai/label_contract.v1.json).

## Repository Structure

```
coding-workflows/
  .github/
    workflows/          # Reusable workflow_call workflows
    actions/
      setup-runtime/    # Shared composite action for runtime setup
    ai/                 # AI config: label contract, orchestrate schema
  scripts/              # Helper scripts (memory, context, git, orchestrator)
  prompts/              # LLM prompt templates (clarify, plan, orchestrate, judge)
  ai-memory/            # Memory schemas, config, and examples
  netwask/              # Agent configuration
  docs/                 # Documentation
```

## Versioning

- **Immutable tags**: `v1.0.0`, `v1.0.1`, etc.
- **Stable channel**: `@stable` — moving tag, updated after canary validation
- **Canary channel**: `@canary` — pre-stable testing

Consumer repos pin to `@stable` for automatic updates or exact tags for reproducibility.

## Contributing

1. Make changes in a feature branch
2. Test via canary channel on pilot repos
3. Promote to stable after validation

See `docs/release-policy.md` for the full release process.
